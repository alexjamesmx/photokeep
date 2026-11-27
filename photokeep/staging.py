"""An import's files staged under cache/import/<import id>/, and exiftool's records of them.

Staging lives in cache/, which is rebuildable: a run cut off anywhere leaves at worst files that are staged again.
"""
import errno
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from photokeep import events, fsops
from photokeep.cache import regular
from photokeep.hashcache import file_sha256
from photokeep.library import Library, LibraryError
from photokeep.sources import exif_records, new_id

# As sources/*/exif.json was made: JSON, and numbers instead of prose (GPS in degrees, durations in seconds).
_EXIFTOOL_FLAGS = ("-j", "-n")


class ImportFailed(Exception):
    """Why an import cannot start or go on, in one line."""


def exiftool() -> str:
    found = shutil.which("exiftool")
    if found is None:
        raise ImportFailed("exiftool is required: install it (e.g. perl-image-exiftool / brew install exiftool)")
    return found


def staged_id(lib: Library, adapter: str, tag: str, now: datetime) -> str:
    """The id of the staging folder an earlier run with this tag left, or a new one dated today."""
    found = sorted(p.name for p in (lib.cache / "import").glob(f"*-{adapter}-{tag}") if p.is_dir())
    return found[0] if found else new_id(adapter, tag, now)


def _epoch(mtime: str | None) -> float | None:
    try:
        moment = datetime.fromisoformat(mtime or "")
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).timestamp()


def _read_record(path: Path) -> dict[str, tuple[str, int]]:
    staged: dict[str, tuple[str, int]] = {}
    try:
        lines = path.read_bytes().splitlines()
    except FileNotFoundError:
        return staged
    for line in lines:
        try:
            entry = json.loads(line)
            staged[entry["path"]] = (entry["sha256"], entry["size"])
        except (ValueError, TypeError, KeyError):  # a line a crash cut off: its file is staged again
            continue
    return staged


class Stage:
    """cache/import/<import id>/<source path>: an import's files, where exiftool reads them and from where they are
    moved into the library. <import id>.jsonl beside the folder has a line per staged file with its SHA-256, so a
    run after a cut-off one keeps what was staged whole."""

    def __init__(self, lib: Library, import_id: str):
        self.top = lib.cache_dir() / "import"
        self.id, self.folder, self.record = import_id, self.top / import_id, self.top / f"{import_id}.jsonl"
        for path in (self.top, self.folder, self.record):
            if path.is_symlink():  # staging writes and deletes there
                raise LibraryError(f"{path} is a symlink; a cache folder must be a real folder")
        self.created = not self.folder.exists()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.staged = _read_record(self.record)

    def intact(self, source_path: str, size: int) -> str | None:
        """The SHA-256 of the file staged for source_path when it is whole: the listed size, the bytes it was
        staged with."""
        known, path = self.staged.get(source_path), self.folder / source_path
        if known and known[1] == size and regular(path) and path.stat().st_size == size \
                and file_sha256(path) == known[0]:
            return known[0]
        return None

    def put(self, source_path: str, read: Callable[[], BinaryIO], mtime: str | None) -> tuple[str, int]:
        """Stage one file, hashed while it is copied and dated like the source (exiftool reports the date);
        its SHA-256 and size."""
        dst = self.folder / source_path
        with read() as fin:
            part, sha = fsops.stage_stream(fin, dst)
        try:
            size = part.stat().st_size
            if (moment := _epoch(mtime)) is not None:
                os.utime(part, (moment, moment))
            os.replace(part, dst)
        finally:
            part.unlink(missing_ok=True)
        self.staged[source_path] = (sha, size)
        line = json.dumps({"path": source_path, "sha256": sha, "size": size}, ensure_ascii=False).encode() + b"\n"
        events.append_lines(self.record, line)  # whole lines only, a torn one cut off first
        return sha, size

    def place(self, source_path: str, read: Callable[[], BinaryIO], size: int, mtime: str | None, sha: str,
              dst: Path) -> None:
        """Move the staged file to dst, never over a file there, once its bytes are checked again; staged anew
        first when they are not the planned ones (a cut-off run moved it, or cache/ was deleted)."""
        if self.intact(source_path, size) != sha and self.put(source_path, read, mtime)[0] != sha:
            raise fsops.HashMismatch(f"{source_path}: the source's bytes changed since this import was planned")
        src = self.folder / source_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(dst):
            raise fsops.CopyError(f"{dst} already exists")
        try:
            os.replace(src, dst)
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            fsops.copy_verified(src, dst, sha)

    def exif(self, tool: str, source_paths: list[str]) -> tuple[bytes, dict[str, dict]]:
        """exiftool's output on these staged files, and its records by source path. The files are named one by one,
        so none is passed over for its extension or a folder starting with a dot."""
        if not source_paths:
            return b"[]", {}
        # One argument per line; source paths hold no line breaks and these never start with a space or "#".
        names = "".join(f"{self.id}/{p}\n" for p in source_paths).encode("utf-8", "surrogateescape")
        done = subprocess.run([tool, *_EXIFTOOL_FLAGS, "-@", "-"], cwd=self.top, input=names, capture_output=True,
                              check=False)
        try:
            return done.stdout, exif_records(done.stdout, self.id)
        except (ValueError, TypeError, KeyError):
            said = done.stderr.decode(errors="replace").strip().splitlines() or [f"exit status {done.returncode}"]
            raise ImportFailed(f"exiftool gave no records: {said[0]}") from None

    def remove(self) -> None:
        self.record.unlink(missing_ok=True)
        shutil.rmtree(self.folder, ignore_errors=True)

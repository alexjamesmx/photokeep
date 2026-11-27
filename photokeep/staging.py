"""An import's work on disk: candidates staged under cache/import/, exiftool's records of them, and the write-once
sources/<import id>/ folder.

Staging lives in cache/, which is rebuildable: a run cut off anywhere leaves at worst files that are staged again.
"""
import errno
import json
import os
import shutil
import subprocess
import time
import zipfile
import zlib
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from photokeep import events, fsops
from photokeep.cache import exif_key, regular
from photokeep.hashcache import file_sha256
from photokeep.library import Library, LibraryError

if TYPE_CHECKING:
    from photokeep.importer import Candidate

Say = Callable[[str], None]
UNREADABLE = (OSError, EOFError, zipfile.BadZipFile, zlib.error)  # a file gone, a damaged zip member
_EVERY = 5.0  # seconds between progress lines
# As sources/*/exif.json was made: JSON, numbers instead of prose (GPS in degrees, durations in seconds), subfolders.
_EXIFTOOL_FLAGS = ("-j", "-n", "-r")


class ImportFailed(Exception):
    """Why an import cannot start or go on, in one line."""


def problem(c: "Candidate", reason: str) -> dict:
    return {"source_path": c.source_path, "reason": reason}


def why(e: BaseException) -> str:
    return fsops.failure(e) if isinstance(e, OSError) else str(e)  # a full disk stops the run


def listing(candidates: "list[Candidate]") -> list[tuple[str, int]]:
    return sorted((c.source_path, c.size) for c in candidates)


def ticking(it: Iterable, total: int, say: Say, what: str) -> Iterator:
    """Yield from it, telling `say` how far it got at most every few seconds: a whole library takes minutes."""
    last = time.monotonic()
    for n, x in enumerate(it, 1):
        yield x
        if n == total or time.monotonic() - last >= _EVERY:
            say(f"{what}: {n}/{total}")
            last = time.monotonic()


def exiftool() -> str:
    found = shutil.which("exiftool")
    if found is None:
        raise ImportFailed("exiftool is required: install it (e.g. perl-image-exiftool / brew install exiftool)")
    return found


def exif_records(raw: bytes, top: str) -> dict[str, dict]:
    """exiftool -j output by source path, top being the folder it ran on. ValueError, TypeError or KeyError when
    raw is not a list of records that each name their SourceFile."""
    return {exif_key(r["SourceFile"], top): r for r in json.loads(raw)}


def import_id_for(lib: Library, adapter: str, tag: str, now: datetime) -> str:
    """The id an earlier run gave this listing (its sources/ or staging folder is still there), or a new one dated
    today: a run after a cut-off one goes on with what that one left."""
    pattern = f"*-{adapter}-{tag}"
    found = (sorted(p.name for p in lib.sources.glob(pattern))
             or sorted(p.name for p in (lib.cache / "import").glob(pattern) if p.is_dir()))
    return found[0] if found else f"{now.astimezone():%Y-%m-%d}-{adapter}-{tag}"


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
    moved into the library. <import id>.jsonl beside the folder, out of exiftool's way (it reads JSON files too),
    has a line per staged file with its SHA-256, so a run after a cut-off one keeps what was staged whole."""

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

    def put(self, source_path: str, read: Callable[[], BinaryIO]) -> tuple[str, int]:
        """Stage one file, hashed while it is copied; its SHA-256 and size."""
        dst = self.folder / source_path
        with read() as fin:
            part, sha = fsops.stage_stream(fin, dst)
        try:
            size = part.stat().st_size
            os.replace(part, dst)
        finally:
            part.unlink(missing_ok=True)
        self.staged[source_path] = (sha, size)
        line = json.dumps({"path": source_path, "sha256": sha, "size": size}, ensure_ascii=False).encode() + b"\n"
        events.append_lines(self.record, line)  # whole lines only, a torn one cut off first
        return sha, size

    def place(self, source_path: str, read: Callable[[], BinaryIO], size: int, sha: str, dst: Path) -> None:
        """Move the staged file to dst, never over a file there, once its bytes are checked again; staged anew
        first when they are not the planned ones (a cut-off run moved it, or cache/ was deleted)."""
        if self.intact(source_path, size) != sha and self.put(source_path, read)[0] != sha:
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

    def exif(self, tool: str) -> tuple[bytes, dict[str, dict]]:
        """exiftool's output on the staged files, and its records by source path."""
        done = subprocess.run([tool, *_EXIFTOOL_FLAGS, self.id], cwd=self.top, capture_output=True, check=False)
        raw = done.stdout or b"[]"  # no file it could read
        try:
            return raw, exif_records(raw, self.id)
        except (ValueError, TypeError, KeyError):
            said = done.stderr.decode(errors="replace").strip().splitlines() or [f"exit status {done.returncode}"]
            raise ImportFailed(f"exiftool gave no records: {said[0]}") from None

    def hashed(self, lib: Library, candidates: "list[Candidate]", tool: str, problems: list[dict],
               say: Say) -> tuple[dict[str, str], bytes, dict[str, dict]]:
        """Each candidate's SHA-256 by source path, exiftool's output and its records: those a cut-off run kept in
        sources/<import id>/, which it planned with, or else of the candidates staged now."""
        kept = lib.sources / self.id
        if kept.is_dir():
            return _recorded(kept, candidates, problems)
        shas = {}
        for c in ticking(candidates, len(candidates), say, "staging"):
            sha = self.intact(c.source_path, c.size)
            if sha is None:
                try:
                    sha, size = self.put(c.source_path, c.read)
                except UNREADABLE as e:
                    problems.append(problem(c, f"could not be read: {why(e)}"))
                    continue
                if size != c.size:
                    problems.append(problem(c, f"{size} bytes read, {c.size} listed"))
                    continue
            shas[c.source_path] = sha
        say("reading EXIF with exiftool")
        raw, exif = self.exif(tool)
        return shas, raw, exif

    def remove(self) -> None:
        self.record.unlink(missing_ok=True)
        shutil.rmtree(self.folder, ignore_errors=True)


def _recorded(folder: Path, candidates: "list[Candidate]",
              problems: list[dict]) -> tuple[dict[str, str], bytes, dict[str, dict]]:
    try:
        manifest = json.loads((folder / "manifest.json").read_bytes())
        listed = {f["source_path"]: (f["sha256"], f["size"]) for f in manifest["files"]}
        raw = (folder / "exif.json").read_bytes()
        exif = exif_records(raw, manifest["exif_root"])
    except (OSError, ValueError, TypeError, KeyError) as e:
        raise ImportFailed(f"{folder} cannot be read ({type(e).__name__}: {e})") from None
    if sorted((path, size) for path, (_, size) in listed.items()) != listing(candidates):
        raise ImportFailed(f"{folder} lists other files than this source")
    hashed = {path: sha for path, (sha, _) in listed.items() if isinstance(sha, str)}
    problems += [problem(c, "could not be read") for c in candidates if c.source_path not in hashed]
    return hashed, raw, exif


def durable(path: Path, data: bytes) -> None:
    """Write a new file and flush it to disk, so a folder renamed into place afterwards never holds a torn one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def keep_sources(lib: Library, import_id: str, manifest: dict, fill: Callable[[Path], None]) -> None:
    """Write sources/<import id>/ whole or not at all: fill puts the provider's files in a temp folder, manifest.json
    goes in last, and one rename makes the folder appear. One already there is complete and stays as it is: import
    folders are write-once."""
    final = lib.sources / import_id
    if final.exists():
        return
    temp = lib.cache_dir() / f"sources-{import_id}"
    shutil.rmtree(temp, ignore_errors=True)  # a cut-off run's half-written folder
    temp.mkdir()  # fails if anything, a symlink say, is still there: never write through it
    fill(temp)
    durable(temp / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1).encode())
    os.rename(temp, final)

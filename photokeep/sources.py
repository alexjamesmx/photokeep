"""Import folders, sources/<import id>/: a provider's files kept verbatim, exiftool's records of the media
(exif.json) and manifest.json. An import writes each whole or not at all, and it is never changed after; sync copies
its files one by one, only adding them, so a copy may hold part of one until a sync finishes.
"""
import json
import os
import shutil
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from photokeep import fsops
from photokeep.cache import exif_key
from photokeep.library import Library


def new_id(adapter: str, tag: str, now: datetime) -> str:
    return f"{now.astimezone():%Y-%m-%d}-{adapter}-{tag}"


def find_folder(lib: Library, adapter: str, tag: str) -> Path | None:
    """The import folder an earlier run with this tag wrote: a run after a cut-off one goes on with it."""
    found = sorted(lib.sources.glob(f"*-{adapter}-{tag}"))
    return found[0] if found else None


def exif_records(raw: bytes, top: str) -> dict[str, dict]:
    """exiftool -j output by source path, top being the folder its paths start with. ValueError, TypeError or
    KeyError when raw is not a list of records that each name their SourceFile."""
    return {exif_key(r["SourceFile"], top): r for r in json.loads(raw)}


def read_folder(folder: Path) -> tuple[dict[str, tuple[str, int]], bytes, dict[str, dict]]:
    """The media an import folder records: SHA-256 and size by source path, exiftool's output and its records.
    OSError, ValueError, TypeError or KeyError when it cannot be read."""
    manifest = json.loads((folder / "manifest.json").read_bytes())
    files = {f["source_path"]: (f["sha256"], f["size"]) for f in manifest["files"]}
    raw = (folder / "exif.json").read_bytes()
    return files, raw, exif_records(raw, manifest["exif_root"])


def durable(path: Path, data: bytes) -> None:
    """Write a new file and flush it to disk, so a folder renamed into place afterwards never holds a torn one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def write_folder(lib: Library, import_id: str, manifest: dict, fill: Callable[[Path], None]) -> None:
    """Write sources/<import id>/ whole or not at all: fill puts the provider's files in a temp folder, manifest.json
    goes in last, and one rename makes the folder appear. One already there is complete and stays as it is."""
    final = lib.sources / import_id
    if final.exists():
        return
    temp = lib.cache_dir() / f"sources-{import_id}"
    shutil.rmtree(temp, ignore_errors=True)  # a cut-off run's half-written folder
    temp.mkdir()  # fails if anything, a symlink say, is still there: never write through it
    fill(temp)
    durable(temp / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1).encode())
    for folder, _, _ in os.walk(temp):  # the files' names, not only their bytes, must survive a power cut
        fsops.fsync_dir(Path(folder))
    os.rename(temp, final)
    fsops.fsync_dir(lib.sources)

"""SHA-256 of the files inside zip archives, read in place: an export's zips are hashed, never extracted."""
import hashlib
import os
import zipfile
import zlib
from collections.abc import Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from functools import cache
from pathlib import Path, PurePosixPath


class ZipScanError(Exception):
    pass


def members(zips: Iterable[Path], skip: tuple[str, ...] = ()) -> list[tuple[Path, str]]:
    """Every file in the zips as (zip, member name), except those whose lower-case extension is in skip."""
    out = []
    for z in zips:
        try:
            with zipfile.ZipFile(z) as zf:
                out += [(z, i.filename) for i in zf.infolist()
                        if not i.is_dir() and PurePosixPath(i.filename).suffix.lower() not in skip]
        except zipfile.BadZipFile as e:
            raise ZipScanError(f"{z}: {e}") from None
    return out


@cache
def _open(path: Path) -> zipfile.ZipFile:
    # Kept open for the worker's life: reading a central directory of thousands of entries per member is slow.
    return zipfile.ZipFile(path)


def _sha(path: Path, name: str) -> str | None:
    try:
        with _open(path).open(name) as f:
            return hashlib.file_digest(f, "sha256").hexdigest()
    except (OSError, EOFError, zipfile.BadZipFile, zlib.error):  # damaged data or a CRC that does not match
        return None


def hashes(found: list[tuple[Path, str]]) -> Iterator[str | None]:
    """Each member's SHA-256 in order, None for one that cannot be read; one member per worker task."""
    try:
        with ProcessPoolExecutor(max(1, (os.cpu_count() or 2) - 2)) as pool:
            yield from pool.map(_sha, [z for z, _ in found], [m for _, m in found])
    except BrokenProcessPool:
        raise ZipScanError("a process hashing the zips died (out of memory?)") from None

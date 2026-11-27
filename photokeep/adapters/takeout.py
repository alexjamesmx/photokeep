"""Lists a Google Photos Takeout for the import pipeline: its zips, read in place, or the folders they were
extracted to. The parts of a split export make one listing, whichever part holds a file or its sidecar.
"""
import re
import zipfile
import zlib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, cast

from photokeep.adapters import Listing, files_under, live_pairs, opener
from photokeep.importer import Candidate, ImportFailed, SourceFile
from photokeep.paths import os_junk
from photokeep.providers.takeout import ALBUM_METADATA, GOOGLE_PHOTOS, original, pair, record

_TOP = "Takeout"
_YEAR = re.compile(r"Photos from \d{4}")  # Google's folder for a year; every other folder is an album
_UNREADABLE = (OSError, EOFError, zipfile.BadZipFile, zlib.error)


@dataclass(frozen=True)
class _File:
    origin: Path  # the zip or folder that holds it
    size: int
    read: Callable[[], BinaryIO]


def _member(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> Callable[[], BinaryIO]:
    return lambda: cast(BinaryIO, zf.open(info))  # a ZipExtFile, which typeshed calls IO[bytes]


def _zip(path: Path) -> Iterator[tuple[str, _File]]:
    try:
        # One handle serves every member's read (ZipFile locks it per read): opening a central directory of
        # thousands of entries once per member would add minutes to an import.
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as e:
        raise ImportFailed(f"{path} cannot be read as a zip: {e}") from None
    for info in zf.infolist():
        top, _, rel = info.filename.partition("/")
        if top == _TOP and not info.is_dir():
            yield rel, _File(path, info.file_size, _member(zf, info))


def _folder(path: Path) -> Iterator[tuple[str, _File]]:
    top = path / _TOP
    if (top / GOOGLE_PHOTOS).is_dir():
        for f, st in files_under(top / GOOGLE_PHOTOS):
            yield f.relative_to(top).as_posix(), _File(path, st.st_size, opener(f))


def _files(sources: Sequence[Path]) -> dict[str, _File]:
    """Every file in a folder of Google Photos/, by its path from Google Photos/ on."""
    files: dict[str, _File] = {}
    for src in sources:
        found = [(p, f) for p, f in (_folder(src) if src.is_dir() else _zip(src))
                 if p.startswith(f"{GOOGLE_PHOTOS}/") and p.count("/") >= 2 and not os_junk(PurePosixPath(p).name)]
        if not found:
            raise ImportFailed(f"{src} holds no {_TOP}/{GOOGLE_PHOTOS}/ files")
        for p, f in found:
            if p in files:
                raise ImportFailed(f"{p} is in both {files[p].origin} and {src}")
            files[p] = f
    return files


def _bytes(path: str, f: _File) -> bytes:
    """A JSON file's bytes. The listing needs them all, so one that cannot be read stops it."""
    try:
        with f.read() as fh:
            return fh.read()
    except _UNREADABLE as e:
        raise ImportFailed(f"{path} could not be read: {e}") from None


def _held(data: bytes) -> Callable[[], bytes]:
    return lambda: data


def _album(folder: str, kept: dict[str, bytes]) -> str | None:
    """The album a folder is (None for a year's): its metadata.json's title, else the folder's name."""
    if _YEAR.fullmatch(folder):
        return None
    meta = kept.get(f"{GOOGLE_PHOTOS}/{folder}/{ALBUM_METADATA}")
    return (record(meta).get("title") if meta else None) or folder


def _name(name: str, title: str | None) -> str:
    """The title is the name the file was uploaded with, which the export may have numbered or cut: kept when it
    names the same kind of file."""
    if title and PurePosixPath(title).suffix.lower() == PurePosixPath(name).suffix.lower():
        return title
    return name


def _top(path: str) -> str:
    return path.split("/")[1]  # Google Photos/<folder>/...


def list(sources: Sequence[Path]) -> Listing:
    """The export's media files as candidates, year folders first, and every JSON file to keep. Takeout dates its
    files by the export, so no candidate has an mtime: the sidecar has the date."""
    files = _files(sources)
    kept = {p: _bytes(p, f) for p, f in files.items() if p.lower().endswith(".json")}
    media = {p: f for p, f in files.items() if p not in kept}
    sidecar_of, clips = pair(media, kept), live_pairs(media)
    albums = {folder: _album(folder, kept) for folder in {_top(p) for p in media}}

    def candidate(path: str) -> Candidate:
        parent, _, name = path.rpartition("/")
        side = sidecar_of.get(path)
        rec = record(kept[side]) if side else {}
        made_from = original(name)  # an edited copy keeps its own name: the sidecar it shares names the original
        edit_of = f"{parent}/{made_from}" if made_from else None
        album = albums[_top(path)]
        return Candidate(path, media[path].read, media[path].size, None,
                         name if made_from else _name(name, rec.get("title")), rec,
                         albums=(album,) if album else (), trashed=(rec.get("raw") or {}).get("trashed") is True,
                         favorite=bool(rec.get("favorite")), live_of=clips.get(path),
                         edit_of=edit_of if edit_of in media else None,
                         note=None if rec or made_from else ("sidecar is not a JSON object" if side else "no sidecar"))

    order = sorted(media, key=lambda p: (albums[_top(p)] is not None, p))
    return [candidate(p) for p in order], [SourceFile(p, _held(d)) for p, d in sorted(kept.items())]

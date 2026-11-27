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
from photokeep.paths import os_junk, utf8
from photokeep.providers.takeout import ALBUM_METADATA, GOOGLE_PHOTOS, original, pair, record

_TOP = "Takeout"
_YEAR = re.compile(r"Photos from \d{4}")  # Google's folder for a year; every other folder is an album
_UNREADABLE = (OSError, EOFError, zipfile.BadZipFile, zlib.error)
_CHUNK = 1 << 20
_MAX_JSON = 16 << 20  # a JSON file is read whole; Google's sidecars are a few hundred bytes


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
    except UnicodeDecodeError:  # a name flagged as UTF-8 that is not
        raise ImportFailed(f"{path} names a file in a way that is not UTF-8") from None
    for info in zf.infolist():
        top, _, rel = info.filename.partition("/")
        if top == _TOP and not info.is_dir():
            yield rel, _File(path, info.file_size, _member(zf, info))


def _folder(path: Path) -> Iterator[tuple[str, _File]]:
    top = path / _TOP
    if (top / GOOGLE_PHOTOS).is_dir():
        for f, st in files_under(top / GOOGLE_PHOTOS):
            yield f.relative_to(top).as_posix(), _File(path, st.st_size, opener(f))


def _same(path: str, a: _File, b: _File) -> bool:
    """Whether two parts hold the same bytes at path, compared as they are read."""
    if a.size != b.size:
        return False
    try:
        with a.read() as fa, b.read() as fb:
            while chunk := fa.read(_CHUNK):
                if fb.read(len(chunk)) != chunk:
                    return False
            return not fb.read(1)
    except _UNREADABLE as e:
        raise ImportFailed(f"{path} could not be read: {e}") from None


def _files(sources: Sequence[Path]) -> dict[str, _File]:
    """Every file under Google Photos/, by its path from Google Photos/ on. One that several parts hold with the
    same bytes is listed once."""
    files: dict[str, _File] = {}
    for src in sources:
        seen: set[str] = set()
        for p, f in _folder(src) if src.is_dir() else _zip(src):
            if not p.startswith(f"{GOOGLE_PHOTOS}/") or os_junk(PurePosixPath(p).name):
                continue
            if p in seen:
                raise ImportFailed(f"{src} holds {p} twice")
            seen.add(p)
            if p not in files:
                files[p] = f
            elif not _same(p, files[p], f):
                raise ImportFailed(f"{p} differs between {files[p].origin} and {src}")
        if not seen:
            raise ImportFailed(f"{src} holds no {_TOP}/{GOOGLE_PHOTOS}/ files")
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


def _album(folder: str | None, kept: dict[str, bytes]) -> str | None:
    """The album a folder is (None for a year's): its metadata.json's title, else the folder's name."""
    if folder is None or _YEAR.fullmatch(folder):
        return None
    meta = kept.get(f"{GOOGLE_PHOTOS}/{folder}/{ALBUM_METADATA}")
    title = record(meta).get("title") if meta else None
    return title if title and utf8(title) else folder


def _name(name: str, title: str | None) -> str:
    """The title is the name the file was uploaded with, which the export may have numbered or cut: kept when it
    names the same kind of file and can be written."""
    if title and utf8(title) and PurePosixPath(title).suffix.lower() == PurePosixPath(name).suffix.lower():
        return title
    return name


def _note(side: str | None, rec: dict, kept: dict[str, bytes]) -> str | None:
    """Why a file has no provider record, which leaves it without Google's date and place."""
    if rec:
        return None
    if side is None:
        return "no sidecar"
    return "sidecar is not a JSON object" if side in kept else "sidecar too large"


def _subfolder(path: str) -> str | None:
    """The folder of Google Photos/ a file is in; None for one directly in Google Photos/."""
    parts = path.split("/")
    return parts[1] if len(parts) > 2 else None


def list(sources: Sequence[Path]) -> Listing:
    """The export's media files as candidates, year folders first, and every JSON file to keep. Takeout dates its
    files by the export, so no candidate has an mtime: the sidecar has the date."""
    files = _files(sources)
    jsons = {p: f for p, f in files.items() if p.lower().endswith(".json")}
    kept = {p: _bytes(p, f) for p, f in jsons.items() if f.size <= _MAX_JSON}
    media = {p: f for p, f in files.items() if p not in jsons}
    sidecar_of, clips = pair(media, jsons), live_pairs(media)
    albums = {folder: _album(folder, kept) for folder in {_subfolder(p) for p in media}}

    def candidate(path: str) -> Candidate:
        name = PurePosixPath(path).name
        side = sidecar_of.get(path)
        rec = record(kept[side]) if side in kept else {}
        made_from = original(path, media)  # an edited copy keeps its own name: its sidecar names the original
        album = albums[_subfolder(path)]
        return Candidate(path, media[path].read, media[path].size, None,
                         name if made_from else _name(name, rec.get("title")), rec,
                         albums=(album,) if album else (), trashed=(rec.get("raw") or {}).get("trashed") is True,
                         favorite=bool(rec.get("favorite")), live_of=clips.get(path),
                         edit_of=made_from if made_from in media else None,
                         note=None if made_from else _note(side, rec, kept))

    order = sorted(media, key=lambda p: (albums[_subfolder(p)] is not None, p))
    return [candidate(p) for p in order], [SourceFile(p, _held(d)) for p, d in sorted(kept.items())]

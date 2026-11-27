"""Adapters list a source for the import pipeline (importer.run): each module's list(sources) gives the source's
media files as candidates and the provider files to keep verbatim."""
import os
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from photokeep.importer import Candidate, SourceFile
from photokeep.paths import os_junk

Listing = tuple[list[Candidate], list[SourceFile]]
PHOTOS = frozenset({".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".webp", ".avif", ".tif", ".tiff", ".bmp",
                    ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf"})
VIDEOS = frozenset({".mov", ".mp4", ".m4v", ".3gp", ".avi", ".mkv", ".mts", ".m2ts", ".mpg", ".mpeg", ".wmv",
                    ".webm"})


def ext(path: str) -> str:
    return PurePosixPath(path).suffix.lower()


def opener(path: Path) -> Callable[[], BinaryIO]:
    return lambda: open(path, "rb")


def live_pairs(paths: Iterable[str]) -> dict[str, str]:
    """The photo each Live clip belongs to, by the clip's path: a video and a photo that are alone in sharing a stem
    in their folder."""
    stems: dict[tuple[str, str], list[str]] = defaultdict(list)
    for path in paths:
        p = PurePosixPath(path)
        stems[(str(p.parent), p.stem)].append(path)
    pairs = {}
    for group in stems.values():
        photos = [p for p in group if ext(p) in PHOTOS]
        videos = [p for p in group if ext(p) in VIDEOS]
        if len(group) == 2 and len(photos) == len(videos) == 1:
            pairs[videos[0]] = photos[0]
    return pairs


def files_under(root: Path) -> Iterator[tuple[Path, os.stat_result]]:
    """Every regular file under root with its stat, in name order, never through a symlink, leaving out what an OS
    drops in folders. A folder that cannot be read raises OSError: a file passed over would never be imported."""
    with os.scandir(root) as found:
        entries = sorted(found, key=lambda e: e.name)
    for entry in entries:
        if os_junk(entry.name) or entry.is_symlink():
            continue
        if entry.is_dir():
            yield from files_under(Path(entry.path))
        elif entry.is_file():
            yield Path(entry.path), entry.stat()

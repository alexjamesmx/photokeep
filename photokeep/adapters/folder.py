"""Lists any folder tree for the import pipeline: its photos and videos, dated by the files themselves. It keeps no
provider files, so cache has no reader for it."""
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from photokeep.adapters import PHOTOS, VIDEOS, Listing, ext, files_under, live_pairs, opener
from photokeep.importer import Candidate, ImportFailed

_MEDIA = PHOTOS | VIDEOS


def _utc(seconds: float) -> str | None:
    try:
        return datetime.fromtimestamp(seconds, UTC).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"
    except (OverflowError, OSError, ValueError):
        return None


def _found(sources: Sequence[Path]) -> dict[str, tuple[Path, os.stat_result]]:
    """Each source's photos and videos by source path: the folder's own name, then the path inside it."""
    found: dict[str, tuple[Path, os.stat_result]] = {}
    for src in sources:
        if not src.is_dir():
            raise ImportFailed(f"{src} is not a folder")
        top = Path(os.path.abspath(src)).name
        for path, st in files_under(src):
            key = f"{top}/{path.relative_to(src).as_posix()}"
            if ext(key) not in _MEDIA:
                continue
            if key in found:
                raise ImportFailed(f"{key} is in two sources: give folders with different names")
            found[key] = path, st
    return found


def list(sources: Sequence[Path]) -> Listing:
    """Every photo and video under the source folders, with no provider metadata; a video sharing its stem and
    folder with a photo alone is that photo's Live clip."""
    found = _found(sources)
    clips = live_pairs(found)
    return [Candidate(p, opener(path), st.st_size, _utc(st.st_mtime), path.name, {},
                      live_of=clips.get(p)) for p, (path, st) in found.items()], []

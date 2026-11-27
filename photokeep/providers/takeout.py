"""Reads the JSON sidecars of a Google Photos Takeout, kept verbatim under sources/<import id>/Google Photos/.

Google has named a file's sidecar after it in several ways over the years. pair() is the one place that decides
which sidecar is whose, for the adapter that lists an export and for this reader of what an import kept.
"""
import json
import math
import re
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from photokeep.metadata import provider_record

GOOGLE_PHOTOS = "Google Photos"
ALBUM_METADATA = "metadata.json"  # an album folder's title; never a sidecar
_SUFFIX = ".supplemental-metadata"
_CUT = 51  # Google cuts a sidecar's whole file name to this many characters
_NUMBERED = re.compile(r"(.*)\((\d+)\)")
_EDITED = "-edited"


def original(name: str) -> str | None:
    """The name of the file an `-edited` copy (in any case) was made from."""
    p = PurePosixPath(name)
    if len(p.stem) > len(_EDITED) and p.stem.lower().endswith(_EDITED):
        return p.stem[:-len(_EDITED)] + p.suffix
    return None


def _exact(name: str) -> str | None:
    return f"{name}{_SUFFIX}.json"


def _numbered(name: str) -> str | None:
    """IMG(1).jpg's sidecar is IMG.jpg.supplemental-metadata(1).json."""
    p = PurePosixPath(name)
    m = _NUMBERED.fullmatch(p.stem)
    return f"{m[1]}{p.suffix}{_SUFFIX}({m[2]}).json" if m else None


def _cut(name: str) -> str | None:
    whole = f"{name}{_SUFFIX}.json"
    return whole[:_CUT - len(".json")] + ".json" if len(whole) > _CUT else None


def _older(name: str) -> str | None:
    return f"{name}.json"


def _older_stem(name: str) -> str | None:
    stem = PurePosixPath(name).stem
    return f"{stem}.json" if stem != name else None


_RULES: tuple[Callable[[str], str | None], ...] = (_exact, _numbered, _cut, _older, _older_stem)  # surest first


def _claim(names: Collection[str], free: set[str]) -> dict[str, str]:
    """Each name's sidecar among free, rule by rule. A sidecar leaves free once taken, and also when two names want
    it: it goes to neither, as a guess could give a photo another one's date and place."""
    found: dict[str, str] = {}
    for rule in _RULES:
        wanted: dict[str, list[str]] = defaultdict(list)
        for name in names:
            if name not in found and (target := rule(name)) in free:
                wanted[target].append(name)
        for target, by in wanted.items():
            free.discard(target)
            if len(by) == 1:
                found[by[0]] = target
    return found


def _in_folder(names: list[str], sidecars: set[str]) -> dict[str, str]:
    """An `-edited` copy is paired after every other file, so it never takes or blocks its original's sidecar, and
    shares that one when it has none of its own."""
    edits = {n: o for n in names if (o := original(n))}
    free = sidecars - {ALBUM_METADATA}
    # An original counts even when not listed: a reader of one import folder sees only the files it recorded.
    found = _claim({n for n in names if n not in edits} | set(edits.values()), free)
    found |= _claim([n for n in edits if n not in found], free)
    found |= {n: found[o] for n, o in edits.items() if n not in found and o in found}
    listed = set(names)
    return {n: s for n, s in found.items() if n in listed}


def pair(media: Iterable[str], sidecars: Iterable[str]) -> dict[str, str]:
    """Each media file's sidecar, by source path: one in the file's folder that no other file can claim, except
    that an `-edited` copy shares its original's."""
    names: dict[str, list[str]] = defaultdict(list)
    jsons: dict[str, set[str]] = defaultdict(set)
    for path in media:
        folder, _, name = path.rpartition("/")
        names[folder].append(name)
    for path in sidecars:
        folder, _, name = path.rpartition("/")
        jsons[folder].add(name)
    return {f"{folder}/{n}": f"{folder}/{s}" for folder, ns in names.items()
            for n, s in _in_folder(ns, jsons.get(folder, set())).items()}


def _utc(value: object) -> str | None:
    """A sidecar time ({"timestamp": seconds since 1970 as text}) as UTC ISO with a Z."""
    seconds = value.get("timestamp") if isinstance(value, dict) else None
    if not isinstance(seconds, str | int) or isinstance(seconds, bool):
        return None
    try:
        moment = datetime.fromtimestamp(int(seconds), UTC)
    except (ValueError, OverflowError, OSError):
        return None
    return moment.replace(tzinfo=None).isoformat() + "Z"


def _degrees(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def _place(value: object) -> tuple[float, float] | None:
    """Latitude and longitude; None for Google's 0.0/0.0, which means no place."""
    if not isinstance(value, dict):
        return None
    lat, lon = _degrees(value.get("latitude")), _degrees(value.get("longitude"))
    if lat is None or lon is None or (lat, lon) == (0.0, 0.0) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def record(data: bytes) -> dict:
    """A sidecar's normalized record (metadata.provider_record), {} when it is not a JSON object. The place is
    Google's, else the one it read from the file."""
    try:
        raw = json.loads(data)
    except (ValueError, RecursionError):  # ValueError covers undecodable bytes
        return {}
    if not isinstance(raw, dict):
        return {}
    lat, lon = _place(raw.get("geoData")) or _place(raw.get("geoDataExif")) or (None, None)
    return provider_record(taken=_utc(raw.get("photoTakenTime")), lat=lat, lon=lon, title=_text(raw.get("title")),
                           description=_text(raw.get("description")), favorite=raw.get("favorited") is True, raw=raw)


def read(source_dir: Path) -> dict[str, dict]:
    """The record of each media file the import folder's manifest lists, by source path. Entries the import could
    not hash (sha256 null) are paired too: pairing goes by name."""
    manifest = json.loads((source_dir / "manifest.json").read_bytes())
    media = [f["source_path"] for f in manifest["files"]
             if isinstance(f, dict) and isinstance(f.get("source_path"), str)]
    sidecars = {p.relative_to(source_dir).as_posix(): p
                for p in (source_dir / GOOGLE_PHOTOS).rglob("*.json") if p.is_file()}
    records = {m: record(sidecars[s].read_bytes()) for m, s in pair(media, sidecars).items()}
    return {m: r for m, r in records.items() if r}

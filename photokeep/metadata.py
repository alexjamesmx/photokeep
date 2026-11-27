"""Display metadata derived from a file's EXIF and the provider's export; never written back."""

import re
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import PurePath

DATE_RE = re.compile(r"(\d{4}):(\d\d):(\d\d) (\d\d):(\d\d):(\d\d)(?:\.\d+)?\s*([+-]\d\d:?\d\d|Z)?")
NAME_RE = re.compile(r"(?<!\d)(\d{4})-(\d\d)-(\d\d)[ _](\d\d)\.(\d\d)\.(\d\d)(?!\d)|(?<!\d)(\d{4})(\d\d)(\d\d)_(\d\d)(\d\d)(\d\d)(?!\d)")
VIDEO_EXTS = {".mov", ".mp4"}


def _datetime(fields: Sequence[str]) -> datetime:
    """A datetime from six digit strings: year, month, day, hour, minute, second."""
    year, month, day, hour, minute, second = map(int, fields)
    return datetime(year, month, day, hour, minute, second)


def _exif_date(value, offset=None) -> tuple[datetime | None, str | None]:
    """Wall-clock datetime and '+hh:mm' (or None) from an EXIF/QuickTime date string."""
    m = DATE_RE.match(str(value or ""))
    if not m or m.group(1) == "0000":
        return None, None
    try:
        dt = _datetime(m.groups()[:6])
    except ValueError:
        return None, None
    tz = m.group(7) or offset
    if tz == "Z":
        tz = "+00:00"
    if tz and ":" not in tz:
        tz = f"{tz[:3]}:{tz[3:]}"
    return dt, tz


def _local(utc: datetime) -> tuple[datetime, str]:
    local = utc.astimezone()
    off = local.strftime("%z")
    return local.replace(tzinfo=None), f"{off[:3]}:{off[3:]}"


def _utc_iso(value: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value or "")
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _name_date(name: str) -> datetime | None:
    m = NAME_RE.search(name)
    if not m:
        return None
    try:
        return _datetime([g for g in m.groups() if g is not None])
    except ValueError:
        return None


def _taken(exif: dict, provider: dict | None, name: str, mtime: str | None, video: bool):
    """Spec 6.3 precedence; returns (wall clock, tz, source) or (None, None, None)."""
    dt, tz = _exif_date(exif.get("DateTimeOriginal"), exif.get("OffsetTimeOriginal"))
    if dt:
        return dt, tz, "exif"
    dt, tz = _exif_date(exif.get("CreationDate"))
    if dt:
        return dt, tz, "quicktime"
    # exiftool -j names both tags CreateDate: a photo's EXIF one is the camera's wall clock, like
    # DateTimeOriginal, and a video's QuickTime one is UTC.
    dt, tz = _exif_date(exif.get("CreateDate"), None if video else exif.get("OffsetTimeDigitized"))
    if dt:
        return (*_local(dt.replace(tzinfo=timezone.utc)), "quicktime") if video else (dt, tz, "exif")
    utc = _utc_iso((provider or {}).get("taken"))
    if utc:
        return *_local(utc), "provider"
    dt = _name_date(name)
    if dt:
        return dt, None, "filename"
    utc = _utc_iso(mtime)
    if utc:
        return *_local(utc), "mtime"
    return None, None, None


def provider_record(*, taken: str | None = None, lat: float | None = None, lon: float | None = None,
                    title: str | None = None, description: str | None = None, favorite: bool = False,
                    hidden: bool = False, albums: Iterable[str] = (), memories: Iterable[str] = (),
                    raw: object = None) -> dict:
    """What every provider reader gives for one file: taken is UTC ISO with a Z, raw the provider's own record."""
    return {"taken": taken, "lat": lat, "lon": lon, "title": title, "description": description, "favorite": favorite,
            "hidden": hidden, "albums": list(albums), "memories": list(memories), "raw": raw}


def derive(exif: dict, provider: dict | None, name: str, mtime: str | None) -> dict:
    """Display metadata; of a provider record (provider_record's keys) only the date and place are read."""
    ext = PurePath(name).suffix.lower()
    is_video = str(exif.get("MIMEType") or "").startswith("video/") or ext in VIDEO_EXTS
    dt, tz, src = _taken(exif, provider, name, mtime, is_video)
    lat, lon = exif.get("GPSLatitude"), exif.get("GPSLongitude")
    if lat is None or lon is None:  # the file's own place wins; a provider's is a fallback
        lat, lon = (provider or {}).get("lat"), (provider or {}).get("lon")
    is_png = str(exif.get("FileType") or "").upper() == "PNG" or ext == ".png"
    return {
        "kind": "video" if is_video else "photo",
        "taken": dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else None,
        "tz": tz,
        "date_src": src,
        "w": exif.get("ImageWidth"),
        "h": exif.get("ImageHeight"),
        "duration": exif.get("Duration"),
        "codec": exif.get("CompressorID"),
        "make": exif.get("Make"),
        "model": exif.get("Model"),
        "lat": lat,
        "lon": lon,
        "screenshot": exif.get("UserComment") == "Screenshot" or (is_png and not exif.get("Model")),
    }


def fingerprint(meta: dict) -> tuple[str, str, int, int] | None:
    taken, model, w, h = meta.get("taken"), meta.get("model"), meta.get("w"), meta.get("h")
    return (taken, model, w, h) if taken and model and w and h else None

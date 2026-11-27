"""Display metadata derived from a file's EXIF and the provider's export; never written back."""

import re
from datetime import datetime, timezone
from pathlib import PurePath

DATE_RE = re.compile(r"(\d{4}):(\d\d):(\d\d) (\d\d):(\d\d):(\d\d)(?:\.\d+)?\s*([+-]\d\d:?\d\d|Z)?")
NAME_RE = re.compile(r"(?<!\d)(\d{4})-(\d\d)-(\d\d)[ _](\d\d)\.(\d\d)\.(\d\d)(?!\d)|(?<!\d)(\d{4})(\d\d)(\d\d)_(\d\d)(\d\d)(\d\d)(?!\d)")
VIDEO_EXTS = {".mov", ".mp4"}


def _exif_date(value, offset=None) -> tuple[datetime | None, str | None]:
    """Wall-clock datetime and '+hh:mm' (or None) from an EXIF/QuickTime date string."""
    m = DATE_RE.match(str(value or ""))
    if not m or m.group(1) == "0000":
        return None, None
    try:
        dt = datetime(*map(int, m.groups()[:6]))
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
    parts = [g for g in m.groups() if g is not None]
    try:
        return datetime(*map(int, parts))
    except ValueError:
        return None


def _taken(exif: dict, provider: dict | None, name: str, mtime: str | None):
    """Spec 6.3 precedence; returns (wall clock, tz, source) or (None, None, None)."""
    dt, tz = _exif_date(exif.get("DateTimeOriginal"), exif.get("OffsetTimeOriginal"))
    if dt:
        return dt, tz, "exif"
    dt, tz = _exif_date(exif.get("CreationDate"))
    if dt:
        return dt, tz, "quicktime"
    utc, _ = _exif_date(exif.get("CreateDate"))
    if utc:
        return *_local(utc.replace(tzinfo=timezone.utc)), "quicktime"
    utc = _utc_iso((provider or {}).get("originalCreationDate"))
    if utc:
        return *_local(utc), "provider"
    dt = _name_date(name)
    if dt:
        return dt, None, "filename"
    utc = _utc_iso(mtime)
    if utc:
        return *_local(utc), "mtime"
    return None, None, None


def derive(exif: dict, provider: dict | None, name: str, mtime: str | None) -> dict:
    dt, tz, src = _taken(exif, provider, name, mtime)
    ext = PurePath(name).suffix.lower()
    is_video = str(exif.get("MIMEType") or "").startswith("video/") or ext in VIDEO_EXTS
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
        "lat": exif.get("GPSLatitude"),
        "lon": exif.get("GPSLongitude"),
        "screenshot": exif.get("UserComment") == "Screenshot" or (is_png and not exif.get("Model")),
    }


def fingerprint(meta: dict) -> tuple[str, str, int, int] | None:
    key = (meta.get("taken"), meta.get("model"), meta.get("w"), meta.get("h"))
    return key if all(key) else None

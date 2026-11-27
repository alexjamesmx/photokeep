import re
import unicodedata
from typing import TypeGuard

from photokeep.state import State

PART = ".photokeep-part"  # suffix of copy_verified's temp files
# What Finder and Explorer leave in the folders they show, compared casefolded; ._<name> is macOS's
# AppleDouble file on drives without extended attributes.
_JUNK = frozenset({".ds_store", "thumbs.db", "desktop.ini"})
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_FOLDER = re.compile(r"[0-9]{4}/[0-9]{2}")
_ID = re.compile(r"[0-9a-f]{64}")
# A file system takes names of 255 bytes (ext4, APFS) or 255 UTF-16 units (exFAT, NTFS), and UTF-8 never needs
# fewer bytes than UTF-16 units. A safe name leaves room for the "_" safe_name may add at either end, the clash
# suffix _unique may add (~ and at most a whole id) and PART.
_MAX_NAME = 255 - 2 - len("~") - 64 - len(PART)


def os_junk(name: str) -> bool:
    """True for a file an operating system drops into a folder; scan never sees one."""
    return name.casefold() in _JUNK or name.startswith("._")


def _size(text: str) -> int:
    return len(text.encode("utf-8", "surrogatepass"))


def _fit(name: str) -> str:
    """name cut to _MAX_NAME UTF-8 bytes between two characters, keeping its extension: a source can name a file
    longer than a library's file system takes, with the suffixes a library adds."""
    if _size(name) <= _MAX_NAME:
        return name
    stem, dot, ext = name.rpartition(".")
    stem, ext = (stem, dot + ext) if stem and _size(dot + ext) < _MAX_NAME else (name, "")
    room, kept = _MAX_NAME - _size(ext), []
    for ch in stem:
        room -= _size(ch)
        if room < 0:
            break
        kept.append(ch)
    cut = "".join(kept)
    return cut + ext if ext else cut.rstrip(". ")


def safe_name(name: str) -> str:
    cleaned = _fit(_FORBIDDEN.sub("_", unicodedata.normalize("NFC", name)).rstrip(". "))
    if os_junk(cleaned):  # scan skips junk, so an original by that name would look missing for ever
        cleaned = "_" + cleaned
    # scan deletes *.photokeep-part files as leftovers, so no original may end that way. Compared
    # casefolded because a case-insensitive drive would alias the temp file onto the original.
    if cleaned.casefold().endswith(PART):
        cleaned += "_"
    return cleaned or "_"


def valid_folder(folder: object) -> bool:
    return folder == "undated" or (isinstance(folder, str) and _FOLDER.fullmatch(folder) is not None)


def folder_of(taken: str | None) -> str:
    """The originals/ folder of an item taken at a wall-clock `YYYY-MM-DDTHH:MM:SS`."""
    month = f"{(taken or '')[:4]}/{(taken or '')[5:7]}"
    return month if valid_folder(month) else "undated"


def valid_id(item_id: object) -> TypeGuard[str]:
    return isinstance(item_id, str) and _ID.fullmatch(item_id) is not None


def _unique(name: str, item_id: str, taken: set[str]) -> str:
    name = safe_name(name)
    if name.casefold() not in taken:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    else:
        ext = "." + ext
    short = f"{stem}~{item_id[:8]}{ext}"
    # Item ids are unique, so the full-id form cannot clash.
    return short if short.casefold() not in taken else f"{stem}~{item_id}{ext}"


def assign(state: State) -> dict[str, dict[str, str]]:
    """Map every live or trashed item to its main (and live clip) path, the same on every copy."""
    taken: dict[str, set[str]] = {}
    paths: dict[str, dict[str, str]] = {}
    # Folder and id come from any copy's log and become path segments: anything else could escape.
    items = sorted((i for i in state.items.values()
                    if i.status != "purged" and valid_id(i.id) and valid_folder(i.folder)),
                   key=lambda i: (i.first_ts, i.id))
    for item in items:
        names = taken.setdefault(item.folder, set())
        base = f"originals/{item.folder}" if item.status == "live" else f"trash/{item.id}"
        paths[item.id] = {}
        # main first so a live clip can never take the main file's name
        for f in sorted(item.files, key=lambda f: f["role"] != "main"):
            name = _unique(f["name"], item.id, names)
            names.add(name.casefold())
            paths[item.id][f["role"]] = f"{base}/{name}"
    return paths

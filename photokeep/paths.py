import re
import unicodedata

from photokeep.state import State

PART = ".photokeep-part"  # suffix of copy_verified's temp files
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_FOLDER = re.compile(r"[0-9]{4}/[0-9]{2}")
_ID = re.compile(r"[0-9a-f]{64}")


def safe_name(name: str) -> str:
    cleaned = _FORBIDDEN.sub("_", unicodedata.normalize("NFC", name)).rstrip(". ")
    # scan deletes *.photokeep-part files as leftovers, so no original may end that way. Compared
    # casefolded because a case-insensitive drive would alias the temp file onto the original.
    if cleaned.casefold().endswith(PART):
        cleaned += "_"
    return cleaned or "_"


def valid_folder(folder: object) -> bool:
    return folder == "undated" or (isinstance(folder, str) and _FOLDER.fullmatch(folder) is not None)


def valid_id(item_id: object) -> bool:
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

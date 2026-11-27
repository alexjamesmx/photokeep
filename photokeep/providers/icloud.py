"""Reads the metadata CSVs of Apple's iCloud Photos export (partN/Photos|Albums|Memories)."""

import csv
from datetime import datetime
from pathlib import Path

# Albums that duplicate the flags already read from Photo Details.
FLAG_ALBUMS = {"Favorites", "Hidden"}
APPLE_DATE = "%A %B %d,%Y %I:%M %p GMT"


def _utc_iso(value: str) -> str | None:
    try:
        return datetime.strptime(value.strip(), APPLE_DATE).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def _read_lists(source_dir: Path, folder: str) -> dict[str, list[str]]:
    """Member file names under one header row; the file stem is the title.
    Apple appends a digit to repeated titles ("Spring 2025", "Spring 20251"), so those are merged."""
    files = [f for f in sorted(source_dir.glob(f"*/{folder}/*.csv")) if f.stem not in FLAG_ALBUMS]
    titles = {f.stem for f in files}

    def canonical(stem: str) -> str:
        bases = [t for t in titles if t != stem and stem.startswith(t) and stem[len(t):].isdigit()]
        return min(bases, key=len) if bases else stem

    members: dict[str, list[str]] = {}
    for f in files:
        title = canonical(f.stem)
        with open(f, newline="", encoding="utf-8-sig") as fh:
            for row in list(csv.reader(fh))[1:]:
                if row and row[0] and title not in members.setdefault(row[0], []):
                    members[row[0]].append(title)
    return members


def read(source_dir: Path) -> dict[str, dict]:
    albums = _read_lists(source_dir, "Albums")
    memories = _read_lists(source_dir, "Memories")
    out: dict[str, dict] = {}
    for f in sorted(source_dir.glob("*/Photos/Photo Details*.csv")):
        part = f.parts[-3]
        with open(f, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                name = row["imgName"]
                out[f"{part}/Photos/{name}"] = {
                    "originalCreationDate": _utc_iso(row.get("originalCreationDate", "")),
                    "favorite": row.get("favorite") == "yes",
                    "hidden": row.get("hidden") == "yes",
                    "albums": list(albums.get(name, [])),
                    "memories": list(memories.get(name, [])),
                    "raw": row,
                }
    return out

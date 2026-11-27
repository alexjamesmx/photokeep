"""The viewer's index, cache/index.sqlite: display rows derived from the folded log, sources/ and the files.

Item rows are rebuilt on every refresh; the media table (what rendering measured) is kept, so only new
items are rendered. Deleting cache/ loses nothing but the time to render again.
"""
import json
import os
import re
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import repeat
from pathlib import Path, PurePosixPath

from photokeep import media
from photokeep.events import read_all, utcnow
from photokeep.library import Library, LibraryError
from photokeep.metadata import derive
from photokeep.paths import assign, valid_id
from photokeep.providers import icloud
from photokeep.state import TRASH_DAYS, Item, State, fold

PROVIDERS = {"icloud": icloud.read}
_ADAPTER = re.compile(r"\d{4}-\d\d-\d\d-([a-z0-9]+)-")  # import id: YYYY-MM-DD-<adapter>-<short random>
_MEDIA = ("w", "h", "sharp", "bright", "noise", "dhash", "thumb", "preview", "error")
_COLUMNS = ("id", "n", "dir", "k", "sc", "s", "w", "h", "d", "c", "lc", "lv", "t", "z", "ds", "cam", "la", "lo",
            "f", "hi", "al", "me", "o", "g", "b", "tr", "ta", "r")
_ITEMS = f"items (id TEXT PRIMARY KEY, {', '.join(_COLUMNS[1:])})"
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS media (id TEXT PRIMARY KEY, w INT, h INT, sharp REAL, bright REAL, noise REAL,
                                  dhash TEXT, thumb INT, preview INT, error TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS {_ITEMS};
"""
# The prototype viewer's item keys; measured sizes win over EXIF, which ignores rotation.
_SELECT = """SELECT i.id, n, dir, k, sc, s, COALESCE(m.w, i.w) AS w, COALESCE(m.h, i.h) AS h, d, c, lc, lv, t, z,
ds, cam, la, lo, f, hi, al, me, o, sharp AS sh, bright AS br, noise AS "no", g, b, COALESCE(thumb, 0) AS th,
COALESCE(preview, 0) AS pv, tr, ta, r FROM items i LEFT JOIN media m ON m.id = i.id"""


@dataclass(frozen=True)
class _Source:
    """What sources/<import id>/ says about one file."""
    path: str | None = None  # e.g. part1/Photos/IMG_1.HEIC
    mtime: str | None = None
    exif: dict = field(default_factory=dict)
    provider: dict | None = None


_UNKNOWN = _Source()
_Sources = dict[str, dict[str, _Source]]  # import id -> sha -> _Source


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _exif_key(source_file: str, import_id: str) -> str:
    """exiftool's SourceFile as a manifest source_path: without the folder exiftool ran in (raw/ or the import's)."""
    parts = PurePosixPath(source_file).parts
    cut = next((i + 1 for i, part in enumerate(parts[:-1]) if part in ("raw", import_id)), 0)
    return "/".join(parts[cut:])


def _adapter(manifest: dict, import_id: str) -> str | None:
    if isinstance(manifest.get("adapter"), str):
        return manifest["adapter"]
    m = _ADAPTER.match(import_id)
    return m.group(1) if m else None


def _provider(folder: Path, adapter: str | None) -> dict[str, dict]:
    read = PROVIDERS.get(adapter) if adapter else None
    try:
        return read(folder) if read else {}
    except (OSError, ValueError, KeyError):  # a damaged export: the files' own metadata still shows
        return {}


def _import_sources(folder: Path) -> dict[str, _Source]:
    """An unreadable manifest gives nothing: the cache never blocks sync or the viewer over a damaged import."""
    manifest = _json(folder / "manifest.json")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        return {}
    records = _json(folder / "exif.json")
    exif = {_exif_key(str(r.get("SourceFile", "")), folder.name): r
            for r in (records if isinstance(records, list) else []) if isinstance(r, dict)}
    provider = _provider(folder, _adapter(manifest, folder.name))
    return {f["sha256"]: _Source(f["source_path"], f.get("mtime"), exif.get(f["source_path"], {}),
                                 provider.get(f["source_path"]))
            for f in manifest["files"]
            if isinstance(f, dict) and isinstance(f.get("sha256"), str) and isinstance(f.get("source_path"), str)}


def _sources(lib: Library) -> _Sources:
    return {d.name: _import_sources(d) for d in sorted(lib.sources.iterdir()) if d.is_dir() and not d.is_symlink()}


def _source(sources: _Sources, item: Item, sha: str) -> _Source:
    """The file as the item's earliest import with a manifest saw it."""
    return next((sources[i][sha] for i in item.imports if sha in sources.get(i, {})), _UNKNOWN)


def _row(item: Item, main_path: str, sources: _Sources) -> dict:
    files = {f["role"]: f for f in item.files}
    main, live = files["main"], files.get("live")
    src = _source(sources, item, main["sha256"])
    clip = _source(sources, item, live["sha256"]) if live else _UNKNOWN
    meta = derive(src.exif, src.provider, main["name"], src.mtime or main.get("mtime"))
    rel = PurePosixPath(main_path)
    return {
        "id": item.id, "n": rel.name, "dir": str(rel.parent), "k": meta["kind"][0], "sc": int(meta["screenshot"]),
        "s": sum(f.get("size") or 0 for f in (main, live) if f), "w": meta["w"], "h": meta["h"],
        "d": meta["duration"], "c": meta["codec"], "lc": clip.exif.get("CompressorID"), "lv": int(live is not None),
        "t": meta["taken"], "z": meta["tz"], "ds": meta["date_src"],
        "cam": " ".join(x for x in (meta["make"], meta["model"]) if x) or None, "la": meta["lat"], "lo": meta["lon"],
        "f": int(item.favorite), "hi": int(item.hidden),
        "al": json.dumps(sorted(name for kind, name in item.albums if kind == "album")),
        "me": json.dumps(sorted(name for kind, name in item.albums if kind == "memory")),
        "o": src.path, "g": None, "b": 0,
        "tr": int(item.status == "trashed"), "ta": item.trashed_at, "r": item.trash_reason,
    }


def _rows(lib: Library, state: State) -> list[dict]:
    sources = _sources(lib)
    return [_row(state.items[i], roles["main"], sources) for i, roles in assign(state).items() if "main" in roles]


def _groups(rows: list[dict], measured: dict[str, sqlite3.Row]) -> dict[str, tuple[str, int]]:
    cand = [{"id": r["id"], "kind": "photo", "dhash": m["dhash"], "taken": r["t"], "screenshot": r["sc"],
             "favorite": r["f"], "w": m["w"] or r["w"], "h": m["h"] or r["h"], "sharp": m["sharp"], "size": r["s"]}
            for r in rows if r["k"] == "p" and not r["tr"] and not r["hi"] and (m := measured.get(r["id"]))]
    return media.similar_groups(cand)


def _days_left(trashed_at: str) -> int:
    days = (datetime.now(UTC) - datetime.fromisoformat(trashed_at)) // timedelta(days=1)
    return max(0, min(TRASH_DAYS, TRASH_DAYS - days))  # a clock ahead on another copy never shows over 30


def _item(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["al"], out["me"] = json.loads(out["al"]), json.loads(out["me"])
    out["left"] = _days_left(out["ta"]) if out["tr"] else None
    return out


def _regular(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()  # a symlink's bytes are not the library's


class Cache:
    def __init__(self, lib: Library):
        self.lib = lib

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        # Autocommit plus WAL, as in the hash cache: the viewer keeps reading while a refresh writes.
        with closing(sqlite3.connect(self.lib.cache_db("index.sqlite"), isolation_level=None, timeout=30)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.executescript(_SCHEMA)
            yield db

    def thumb_path(self, item_id: str) -> Path:
        return self._out("thumbs", item_id)

    def preview_path(self, item_id: str) -> Path:
        return self._out("previews", item_id)

    def _out(self, folder: str, item_id: str) -> Path:
        if not valid_id(item_id):  # ids reach here from viewer URLs
            raise ValueError(f"not an item id: {item_id!r}")
        return media.cache_path(folder, item_id, self.lib.cache)

    def _refuse_linked_folders(self) -> None:
        """Thumbnails are written and deleted in these folders; a symlinked one would send that elsewhere."""
        for top in (self.lib.cache / "thumbs", self.lib.cache / "previews"):
            for path in [top] if top.is_symlink() or not top.is_dir() else top.iterdir():
                if path.is_symlink():
                    raise LibraryError(f"{path} is a symlink; a cache folder must be a real folder")

    def refresh(self, render: bool = True, workers: int | None = None) -> dict:
        ok, missing = media.available()
        rendered = errors = 0
        with self._db() as db:
            self._refuse_linked_folders()
            state = fold(read_all(self.lib))
            rows = _rows(self.lib, state)
            self._prune(db, state)
            todo = self._unrendered(db, rows)
            if render and ok and todo:
                errors = self._render(db, todo, workers)
                rendered = len(todo) - errors
                rows = _rows(self.lib, fold(read_all(self.lib)))  # the log may have moved on during a long render
            measured = {m["id"]: m for m in db.execute("SELECT id, w, h, sharp, dhash FROM media WHERE dhash NOT NULL")}
            self._write(db, rows, {} if "numpy" in missing else _groups(rows, measured))
        return {"items": len(rows), "rendered": rendered, "errors": errors,
                "skipped_render": 0 if render and ok else len(todo)}

    def _prune(self, db: sqlite3.Connection, state: State) -> None:
        """Forget what was rendered for purged items: deleted forever includes their thumbnails."""
        gone = [i for (i,) in db.execute("SELECT id FROM media")
                if i in state.tombstones or (i in state.items and state.items[i].status == "purged")]
        for item_id in gone:
            self.thumb_path(item_id).unlink(missing_ok=True)
            self.preview_path(item_id).unlink(missing_ok=True)
        db.executemany("DELETE FROM media WHERE id = ?", [(i,) for i in gone])

    def _unrendered(self, db: sqlite3.Connection, rows: list[dict]) -> list[dict]:
        known = {i for (i,) in db.execute("SELECT id FROM media")}
        return [r for r in rows if (r["id"] not in known or not self.thumb_path(r["id"]).exists())
                and _regular(self.lib.root / r["dir"] / r["n"])]

    def _render(self, db: sqlite3.Connection, todo: list[dict], workers: int | None) -> int:
        """Render todo in worker processes, storing each result as it arrives; return the number of errors."""
        paths = [self.lib.root / r["dir"] / r["n"] for r in todo]
        jobs = ([r["id"] for r in todo], paths, ["video" if r["k"] == "v" else "photo" for r in todo],
                [p.suffix.lower() for p in paths], [r["d"] for r in todo], repeat(self.lib.cache_dir()))
        errors = 0
        with ProcessPoolExecutor(workers or max(1, (os.cpu_count() or 2) - 2)) as pool:
            for r, res in zip(todo, pool.map(media.render, *jobs, chunksize=8), strict=True):
                db.execute(f"INSERT OR REPLACE INTO media (id, {', '.join(_MEDIA)}) VALUES (?{', ?' * len(_MEDIA)})",
                           (r["id"], *(res.get(c) for c in _MEDIA)))
                errors += "error" in res
        return errors

    def _write(self, db: sqlite3.Connection, rows: list[dict], groups: dict[str, tuple[str, int]]) -> None:
        for r in rows:
            r["g"], r["b"] = groups.get(r["id"], (None, 0))
        db.execute("BEGIN IMMEDIATE")
        with db:  # one transaction: readers see the old rows or the new, never a mix
            db.execute("DROP TABLE items")  # recreated, so a newer app's columns need no migration
            db.execute(f"CREATE TABLE {_ITEMS}")
            db.executemany(f"INSERT INTO items VALUES ({', '.join('?' * len(_COLUMNS))})",
                           ([r[c] for c in _COLUMNS] for r in rows))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('refreshed_at', ?)", (utcnow(),))

    def items_json(self) -> list[dict]:
        with self._db() as db:
            return [_item(r) for r in db.execute(f"{_SELECT} ORDER BY t DESC, i.id DESC")]

    def item_json(self, item_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute(f"{_SELECT} WHERE i.id = ?", (item_id,)).fetchone()
        return _item(row) if row else None

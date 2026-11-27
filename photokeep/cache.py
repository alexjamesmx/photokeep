"""The viewer's index, cache/index.sqlite: display rows derived from the folded log, sources/ and the files.

Item rows are rebuilt on every refresh; the media table (what rendering measured) is kept, so only new
items are rendered. Deleting cache/ loses nothing but the time to render again.

sources/ holds third-party exports verbatim and is never edited, so anything in it that cannot be read
is counted and skipped: a damaged export must not stop sync or the viewer for good.
"""
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
_DIED = {"error": "BrokenProcessPool: the renderer process died on this file"}


@dataclass(frozen=True)
class _Source:
    """What sources/<import id>/ says about one file."""
    path: str | None = None  # e.g. part1/Photos/IMG_1.HEIC
    mtime: str | None = None
    exif: dict = field(default_factory=dict)
    provider: dict | None = None


_UNKNOWN = _Source()
_Sources = dict[str, dict[str, _Source]]  # import id -> sha -> _Source
_Read = tuple[dict[str, _Source], int]  # one import's files by sha, and its unreadable metadata files


def _str(value) -> str | None:
    return value if isinstance(value, str) else None


def _json(path: Path, missing):
    """Parsed JSON, `missing` when there is no such file, None when it cannot be read."""
    if not path.exists():
        return missing
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def exif_key(source_file: str, import_id: str) -> str:
    """exiftool's SourceFile as a manifest source_path: without the folder exiftool ran in (raw/ or the import's)."""
    parts = PurePosixPath(source_file).parts
    cut = next((i + 1 for i, part in enumerate(parts[:-1]) if part in ("raw", import_id)), 0)
    return "/".join(parts[cut:])


def _adapter(manifest: dict, import_id: str) -> str | None:
    if isinstance(manifest.get("adapter"), str):
        return manifest["adapter"]
    m = _ADAPTER.match(import_id)
    return m.group(1) if m else None


def _provider(folder: Path, adapter: str | None) -> dict | None:
    """The adapter's records by source path; None when its reader failed."""
    read = PROVIDERS.get(adapter) if adapter else None
    try:
        records = read(folder) if read else {}
    except Exception:  # any failure on third-party content
        return None
    return records if isinstance(records, dict) else None


def _import_sources(folder: Path) -> _Read:
    """The import's files by sha, and how many of its metadata files could not be read."""
    manifest = _json(folder / "manifest.json", None)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        return {}, 1
    records = _json(folder / "exif.json", [])
    provider = _provider(folder, _adapter(manifest, folder.name))
    errors = int(not isinstance(records, list)) + int(provider is None)
    exif = {exif_key(str(r.get("SourceFile", "")), folder.name): r
            for r in (records if isinstance(records, list) else []) if isinstance(r, dict)}
    provider = provider or {}
    files = {f["sha256"]: _Source(f["source_path"], _str(f.get("mtime")), exif.get(f["source_path"], {}),
                                  provider.get(f["source_path"]))
             for f in manifest["files"]
             if isinstance(f, dict) and isinstance(f.get("sha256"), str) and isinstance(f.get("source_path"), str)}
    return files, errors


def _stamp(folder: Path) -> tuple[tuple[str, int, int], ...]:
    """Every file under folder with its size and mtime: it changes whenever what _import_sources reads does."""
    files = []
    for parent, _, names in os.walk(folder):
        for name in names:
            path = os.path.join(parent, name)
            try:
                st = os.stat(path)
            except OSError:  # a sync's temp file, renamed meanwhile
                continue
            files.append((path, st.st_size, st.st_mtime_ns))
    return tuple(sorted(files))


def _sources(lib: Library, load: Callable[[Path], _Read], imports: set[str] | None = None) -> tuple[_Sources, int]:
    """What sources/ says, about every import or only the named ones."""
    found = {d.name: load(d) for d in sorted(lib.sources.iterdir())
             if d.is_dir() and not d.is_symlink() and (imports is None or d.name in imports)}
    return {i: files for i, (files, _) in found.items()}, sum(errors for _, errors in found.values())


def _source(sources: _Sources, item: Item, sha: str) -> _Source:
    """The file as the item's earliest import with a manifest saw it."""
    return next((sources[i][sha] for i in item.imports if sha in sources.get(i, {})), _UNKNOWN)


def _row(item: Item, main_path: str, sources: _Sources) -> dict:
    files = {f["role"]: f for f in item.files}
    main, live = files["main"], files.get("live")
    src = _source(sources, item, main["sha256"])
    clip = _source(sources, item, live["sha256"]) if live else _UNKNOWN
    meta = derive(src.exif, src.provider, main["name"], src.mtime or _str(main.get("mtime")))
    rel = PurePosixPath(main_path)
    # exiftool -j writes numeric-looking text (a Model of "5") as a JSON number
    cam = " ".join(str(x) for x in (meta["make"], meta["model"]) if x) or None
    return {
        "id": item.id, "n": rel.name, "dir": str(rel.parent), "k": meta["kind"][0], "sc": int(meta["screenshot"]),
        "s": sum(f["size"] for f in (main, live) if f and isinstance(f.get("size"), int)),
        "w": meta["w"], "h": meta["h"], "d": meta["duration"], "c": meta["codec"],
        "lc": clip.exif.get("CompressorID"), "lv": int(live is not None),
        "t": meta["taken"], "z": meta["tz"], "ds": meta["date_src"], "cam": cam, "la": meta["lat"], "lo": meta["lon"],
        "f": int(item.favorite), "hi": int(item.hidden),
        "al": json.dumps(sorted(name for kind, name in item.albums if kind == "album")),
        "me": json.dumps(sorted(name for kind, name in item.albums if kind == "memory")),
        "o": src.path, "g": None, "b": 0,
        "tr": int(item.status == "trashed"), "ta": item.trashed_at, "r": item.trash_reason,
    }


def _rows(lib: Library, state: State, load: Callable[[Path], _Read],
          only: set[str] | None = None) -> tuple[list[dict], int]:
    """One row per item (or per item in `only`), and the number of source problems met (unreadable
    files, unusable metadata)."""
    paths = assign(state)  # of every item: one item's path depends on the others' names
    if only is not None:
        paths = {i: roles for i, roles in paths.items() if i in only}
    imports = None if only is None else {i for item_id in paths for i in state.items[item_id].imports}
    sources, errors = _sources(lib, load, imports)
    rows = []
    for item_id, roles in paths.items():
        if "main" not in roles:
            continue
        item = state.items[item_id]
        try:
            rows.append(_row(item, roles["main"], sources))
        except Exception:  # metadata of an unexpected shape: show what the log alone knows
            rows.append(_row(item, roles["main"], {}))
            errors += 1
    return rows, errors


def _candidates(rows: list[dict], measured: dict[str, sqlite3.Row]) -> list[dict]:
    """similar_groups' input: live, visible photos with a dHash, by id so the same input gives the same groups."""
    return sorted(({"id": r["id"], "kind": "photo", "dhash": m["dhash"], "taken": r["t"], "screenshot": r["sc"],
                    "favorite": r["f"], "w": m["w"] or r["w"], "h": m["h"] or r["h"], "sharp": m["sharp"],
                    "size": r["s"]}
                   for r in rows if r["k"] == "p" and not r["tr"] and not r["hi"] and (m := measured.get(r["id"]))),
                  key=lambda c: c["id"])


def _groups(db: sqlite3.Connection, cand: list[dict]) -> tuple[dict[str, tuple[str, int]], str]:
    """Group similar photos, and the digest of the input. Comparing every pair takes seconds for a whole
    library, so unchanged input reuses the groups the last refresh stored."""
    digest = hashlib.sha256(json.dumps(cand).encode()).hexdigest()
    stored = db.execute("SELECT value FROM meta WHERE key = 'groups_input'").fetchone()
    if stored and stored[0] == digest:
        return {i: (g, b) for i, g, b in db.execute("SELECT id, g, b FROM items WHERE g NOT NULL")}, digest
    return media.similar_groups(cand), digest


def _days_left(trashed_at: str | None) -> int | None:
    try:
        at = datetime.fromisoformat(trashed_at or "")
    except ValueError:
        return None
    if at.tzinfo is None:  # every copy writes UTC with Z; anything else is not a time this can count from
        return None
    days = (datetime.now(UTC) - at) // timedelta(days=1)
    return max(0, min(TRASH_DAYS, TRASH_DAYS - days))  # a clock ahead on another copy never shows over 30


def regular(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()  # a symlink's bytes are not the library's


class Cache:
    def __init__(self, lib: Library):
        self.lib = lib
        # import id -> (_stamp, what was read). The viewer keeps one Cache for its whole run, so an edit
        # does not parse its import's whole exif.json (tens of MB for a library) again.
        self._read: dict[str, tuple[tuple, _Read]] = {}

    def _import(self, folder: Path) -> _Read:
        stamp = _stamp(folder)
        known = self._read.get(folder.name)
        if known is None or known[0] != stamp:
            known = self._read[folder.name] = (stamp, _import_sources(folder))
        return known[1]

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
            rows, source_errors = _rows(self.lib, state, self._import)
            self._prune(db, state)
            todo = self._unrendered(db, rows)
            if render and ok and todo:
                errors = self._render(db, todo, workers)
                rendered = len(todo) - errors
                # the log may have moved on meanwhile
                rows, source_errors = _rows(self.lib, fold(read_all(self.lib)), self._import)
            measured = {m["id"]: m for m in db.execute("SELECT id, w, h, sharp, dhash FROM media WHERE dhash NOT NULL")}
            groups, digest = ({}, None) if "numpy" in missing else _groups(db, _candidates(rows, measured))
            self._write(db, rows, groups, digest)
        return {"items": len(rows), "rendered": rendered, "errors": errors,
                "skipped_render": 0 if render and ok else len(todo), "source_errors": source_errors}

    def _prune(self, db: sqlite3.Connection, state: State) -> None:
        """Forget what was rendered for purged items: deleted forever includes their thumbnails."""
        gone = [i for (i,) in db.execute("SELECT id FROM media")
                if i in state.tombstones or (i in state.items and state.items[i].status == "purged")]
        for item_id in gone:
            self.thumb_path(item_id).unlink(missing_ok=True)
            self.preview_path(item_id).unlink(missing_ok=True)
        db.executemany("DELETE FROM media WHERE id = ?", [(i,) for i in gone])

    def seed_media(self, measured: dict[str, dict]) -> None:
        """Store what was measured elsewhere (the prototype library), keyed by item id, so it is not rendered again."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            with db:
                for item_id, res in measured.items():
                    _store(db, item_id, res)

    def _unrendered(self, db: sqlite3.Connection, rows: list[dict]) -> list[dict]:
        known = {i for (i,) in db.execute("SELECT id FROM media")}
        return [r for r in rows if (r["id"] not in known or not self.thumb_path(r["id"]).exists())
                and regular(self.lib.root / r["dir"] / r["n"])]

    def _render(self, db: sqlite3.Connection, todo: list[dict], workers: int | None) -> int:
        """Render todo in worker processes, storing each result as it arrives; return the number of errors."""
        jobs = [self._job(r) for r in todo]
        errors = done = 0
        try:
            with ProcessPoolExecutor(workers or max(1, (os.cpu_count() or 2) - 2)) as pool:
                for job, res in zip(jobs, pool.map(media.render, *zip(*jobs, strict=True), chunksize=8), strict=True):
                    errors += _store(db, job[0], res)
                    done += 1
        except BrokenProcessPool:
            # A worker died (out of memory, a crashing decoder) and took its batch with it. The rest go one at a
            # time, so a file that kills its worker again is known and recorded instead of stopping every refresh.
            errors += self._render_one_by_one(db, jobs[done:])
        return errors

    def _job(self, row: dict) -> tuple:
        """media.render's arguments for one row."""
        path = self.lib.root / row["dir"] / row["n"]
        return row["id"], path, "video" if row["k"] == "v" else "photo", path.suffix.lower(), row["d"], self.lib.cache

    def _render_one_by_one(self, db: sqlite3.Connection, jobs: list[tuple]) -> int:
        errors = 0
        pool = ProcessPoolExecutor(1)
        try:
            for job in jobs:
                try:
                    res = pool.submit(media.render, *job).result()
                except BrokenProcessPool:
                    res = _DIED
                    pool.shutdown()
                    pool = ProcessPoolExecutor(1)
                errors += _store(db, job[0], res)
        finally:
            pool.shutdown()
        return errors

    def _write(self, db: sqlite3.Connection, rows: list[dict], groups: dict[str, tuple[str, int]],
               digest: str | None) -> None:
        for r in rows:
            r["g"], r["b"] = groups.get(r["id"], (None, 0))
        db.execute("BEGIN IMMEDIATE")
        with db:  # one transaction: readers see the old rows or the new, never a mix
            db.execute("DROP TABLE items")  # recreated, so a newer app's columns need no migration
            db.execute(f"CREATE TABLE {_ITEMS}")
            db.executemany(f"INSERT INTO items VALUES ({', '.join('?' * len(_COLUMNS))})",
                           ([r[c] for c in _COLUMNS] for r in rows))
            # the digest is stored with the groups it produced, so reusing them is always consistent
            db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                           [("refreshed_at", utcnow()), ("groups_input", digest)])

    def _item(self, row: sqlite3.Row) -> dict:
        out = dict(row)
        out["al"], out["me"] = json.loads(out["al"]), json.loads(out["me"])
        out["left"] = _days_left(out["ta"]) if out["tr"] else None
        # a deleted cache/thumbs must not show as broken images
        out["th"] = int(bool(out["th"]) and self.thumb_path(out["id"]).exists())
        out["pv"] = int(bool(out["pv"]) and self.preview_path(out["id"]).exists())
        return out

    def refresh_items(self, ids: Iterable[str]) -> None:
        """Rebuild only these items' rows, keeping their similarity groups: after a viewer edit to a few
        items, a full refresh would regroup the whole library."""
        ids = set(ids)
        state = fold(read_all(self.lib))
        rows, _ = _rows(self.lib, state, self._import, ids)
        with self._db() as db:
            self._refuse_linked_folders()
            self._prune(db, state)
            db.execute("BEGIN IMMEDIATE")
            with db:
                for r in rows:
                    old = db.execute("SELECT g, b FROM items WHERE id = ?", (r["id"],)).fetchone()
                    r["g"], r["b"] = (old["g"], old["b"]) if old else (None, 0)
                db.executemany("DELETE FROM items WHERE id = ?", [(i,) for i in ids])
                db.executemany(f"INSERT INTO items VALUES ({', '.join('?' * len(_COLUMNS))})",
                               ([r[c] for c in _COLUMNS] for r in rows))

    def items_json(self, ids: Iterable[str] | None = None) -> list[dict]:
        """Every item's row, newest first; with ids, only those items' rows."""
        with self._db() as db:
            if ids is None:
                return [self._item(r) for r in db.execute(f"{_SELECT} ORDER BY t DESC, i.id DESC")]
            wanted = list(ids)
            # in chunks: SQLite caps the number of parameters in one statement
            chunks = (wanted[k:k + 500] for k in range(0, len(wanted), 500))
            return [self._item(r) for chunk in chunks
                    for r in db.execute(f"{_SELECT} WHERE i.id IN ({', '.join('?' * len(chunk))})", chunk)]

    def item_json(self, item_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute(f"{_SELECT} WHERE i.id = ?", (item_id,)).fetchone()
        return self._item(row) if row else None


def _store(db: sqlite3.Connection, item_id: str, res: dict) -> int:
    """Store one render result; 1 when it is an error."""
    db.execute(f"INSERT OR REPLACE INTO media (id, {', '.join(_MEDIA)}) VALUES (?{', ?' * len(_MEDIA)})",
               (item_id, *(res.get(c) for c in _MEDIA)))
    return int("error" in res)

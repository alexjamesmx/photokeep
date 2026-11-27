"""One-time migration of the prototype library (an organized iCloud export) into an empty copy (spec 6.11).

The prototype, the export's raw/ folder, exif.json and the zips are only read: files are copied with hash
verification, so the prototype keeps working until the owner deletes it.
"""
import json
import os
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from photokeep import __version__, events, fsops, zipscan
from photokeep.cache import Cache, exif_key, regular
from photokeep.hashcache import HashCache, file_sha256
from photokeep.library import Library, locked
from photokeep.metadata import VIDEO_EXTS, derive, fingerprint
from photokeep.paths import assign, valid_folder
from photokeep.providers import icloud
from photokeep.state import fold

Say = Callable[[str], None]
_EVERY = 5.0  # seconds between progress lines
_NOT_MEDIA = (".csv", ".zip")
_MEASURED = ("w", "h", "sharp", "bright", "noise", "dhash")


class MigrationError(Exception):
    pass


@dataclass
class _Item:
    row: dict  # the prototype row whose files, date and thumbnails are kept
    folder: str
    files: list[dict]  # as the import event lists them
    old: dict[str, Path]  # role -> the file in the prototype
    sources: list[dict]  # manifest entries
    fingerprint: list | None
    favorite: bool
    hidden: bool
    albums: list[tuple[str, str]]  # (kind, title)
    trashed: bool

    @property
    def id(self) -> str:
        return self.row["sha256"]


def _ticking(it: Iterable, total: int, say: Say, what: str) -> Iterator:
    """Yield from it, telling `say` how far it got at most every few seconds: a whole library takes minutes."""
    last = time.monotonic()
    for n, x in enumerate(it, 1):
        yield x
        if n == total or time.monotonic() - last >= _EVERY:
            say(f"{what}: {n}/{total}")
            last = time.monotonic()


def _rows(db_path: Path) -> list[dict]:
    if not db_path.is_file():
        raise MigrationError(f"{db_path} is missing: not a prototype library")
    # Read-only, so the prototype's database comes out of this unchanged.
    with closing(sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute("SELECT * FROM items ORDER BY id")]


def _exif(raw_dir: Path, import_id: str) -> dict[str, dict]:
    """exiftool's records by source path (partN/Photos/<name>), keyed as the cache will read them from sources/."""
    path = raw_dir.parent / "exif.json"
    if not raw_dir.is_dir() or not path.is_file():
        raise MigrationError(f"{raw_dir} must be the export's raw/ folder, with exif.json beside it")
    try:
        return {exif_key(r["SourceFile"], import_id): r for r in json.loads(path.read_bytes())}
    except (ValueError, TypeError, KeyError) as e:  # bad JSON, or not a list of records with a SourceFile
        raise MigrationError(f"{path} is not exiftool -j output ({type(e).__name__}: {e})") from None


def _titles(row: dict, col: str) -> list[str]:
    try:
        titles = json.loads(row[col])
        if isinstance(titles, list) and all(isinstance(t, str) for t in titles):
            return titles
    except (TypeError, ValueError):
        pass
    raise MigrationError(f"row {row['id']}: {col} is not a JSON list of titles")


def _clips(exif: dict[str, dict]) -> dict[tuple[str, str], str]:
    """Live Photo clips by (folder, ContentIdentifier): Apple gives a photo and its clip the same identifier."""
    out: dict[tuple[str, str], str] = {}
    for key in sorted(exif):
        cid = exif[key].get("ContentIdentifier")
        if cid and PurePosixPath(key).suffix.lower() in VIDEO_EXTS:
            out.setdefault((key.rpartition("/")[0], cid), key)
    return out


def _sha_size(path: Path) -> tuple[str, int] | None:
    try:
        return file_sha256(path), path.stat().st_size
    except OSError:
        return None


def _hash_files(paths: list[Path], say: Say) -> dict[Path, tuple[str, int] | None]:
    # hashlib releases the GIL, so threads read several files at once.
    with ThreadPoolExecutor(8) as pool:
        hashed = _ticking(pool.map(_sha_size, paths), len(paths), say, "hashing the prototype's files")
        return dict(zip(paths, hashed, strict=True))


def _problem(row: dict, root: Path, hashed: dict) -> str | None:
    if "/" not in (row["orig"] or ""):
        return "no original name"
    main = hashed[root / row["path"]]
    if main is None:
        return "file missing or unreadable"
    if main[0] != row["sha256"]:
        return f"SHA-256 is {main[0]}, the database says {row['sha256']}"
    if row["live"]:
        clip = hashed[root / row["live"]]
        if clip is None:
            return "live clip missing or unreadable"
        if clip[1] != row["live_size"]:  # the prototype kept no hash of clips, only their size
            return f"live clip is {clip[1]} bytes, the database says {row['live_size']}"
    return None


def _kept(group: list[dict]) -> dict:
    """The row whose name, date, clip and thumbnails the item keeps: one with a clip, so none is lost when
    another row has none, then one outside the trash."""
    return min(group, key=lambda r: (not r["live"], bool(r["trashed"]), r["id"]))


def _item(group: list[dict], root: Path, hashed: dict, exif: dict, clips: dict, provider: dict) -> _Item:
    row = _kept(group)
    part, name = row["orig"].split("/", 1)
    source = f"{part}/Photos/{name}"
    roles = [("main", source, root / row["path"])]
    if row["live"]:
        cid = exif.get(source, {}).get("ContentIdentifier")
        guess = f"{part}/Photos/{PurePosixPath(name).stem}{PurePosixPath(row['live']).suffix.upper()}"
        roles.append(("live", clips.get((f"{part}/Photos", cid), guess), root / row["live"]))
    files, sources = [], []
    for role, src, path in roles:
        digest, size = hashed[path]
        files.append({"role": role, "name": src.rpartition("/")[2], "sha256": digest, "size": size, "mtime": None})
        sources.append({"source_path": src, "sha256": digest, "size": size, "mtime": None})
    folder = f"{(row['taken'] or '')[:4]}/{(row['taken'] or '')[5:7]}"
    fp = fingerprint(derive(exif.get(source, {}), provider.get(source), name, None))
    albums: set[tuple[str, str]] = set()
    for r in group:
        albums |= {("album", t) for t in _titles(r, "albums")} | {("memory", t) for t in _titles(r, "memories")}
    return _Item(row, folder if valid_folder(folder) else "undated", files, {r: p for r, _, p in roles}, sources,
                 list(fp) if fp else None, any(r["favorite"] for r in group), any(r["hidden"] for r in group),
                 sorted(albums), all(r["trashed"] for r in group))


def _source(row: dict) -> str:
    return f"row {row['id']} ({row['path']})"


def _items(rows: list[dict], root: Path, hashed: dict, exif: dict, provider: dict, problems: list) -> list[_Item]:
    """One item per distinct main file; rows that cannot be migrated as they are become problems."""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        if problem := _problem(row, root, hashed):
            problems.append({"source": _source(row), "problem": problem})
        else:
            groups.setdefault(row["sha256"], []).append(row)
    clips = _clips(exif)
    items = [_item(group, root, hashed, exif, clips, provider) for group in groups.values()]
    for it, group in zip(items, groups.values()):
        kept = {f["sha256"] for f in it.files}
        for r in (r for r in group if r is not it.row):
            problems.append({"source": _source(r), "problem": f"same bytes as row {it.row['id']}; merged into it"})
            if r["live"] and hashed[root / r["live"]][0] not in kept:
                problems.append({"source": _source(r), "problem": f"live clip {r['live']} not migrated "
                                                                  f"(duplicate of row {it.row['id']})"})
    return items


def _tombstones(members: list[tuple[Path, str]], known: set[str], say: Say, problems: list) -> dict[str, str]:
    """Media in the export whose bytes the library does not hold (deleted in the prototype or in Recently
    Deleted), by SHA-256, with its file name."""
    found: dict[str, str] = {}
    shas = _ticking(zipscan.hashes(members), len(members), say, "hashing the zips")
    for (z, member), digest in zip(members, shas, strict=True):
        if digest is None:
            problems.append({"source": f"{z.name}:{member}", "problem": "unreadable zip member"})
        elif digest not in known:
            found.setdefault(digest, PurePosixPath(member).name)
    return found


def _utc(local: str | None, now: datetime) -> datetime:
    """The prototype wrote trashed_at in this machine's local time, without a zone."""
    try:
        return datetime.fromisoformat(local or "").astimezone(UTC)
    except ValueError:
        return now


def _events(dest: Library, import_id: str, items: list[_Item], tombstones: dict[str, str], now: datetime) -> list[dict]:
    plain: list[tuple[str, dict]] = []
    trash: list[tuple[datetime, dict]] = []
    for it in items:
        plain.append(("import", {"item": it.id, "import": import_id, "folder": it.folder, "files": it.files,
                                 "fingerprint": it.fingerprint, "version_of": None}))
        plain += [(op, {"item": it.id}) for op, on in (("favorite", it.favorite), ("hide", it.hidden)) if on]
        plain += [("album.add", {"item": it.id, "kind": kind, "album": title}) for kind, title in it.albums]
        if it.trashed:
            reason = {"reason": it.row["reason"]} if it.row["reason"] else {}
            trash.append((_utc(it.row["trashed_at"], now), {"item": it.id, **reason}))
    plain += [("purge", {"item": digest, "name": name}) for digest, name in tombstones.items()]
    # A trash keeps the prototype's time, so its 30 days run on; everything else must fold before any trash.
    start = min([now, *(t for t, _ in trash)]) - timedelta(seconds=1, microseconds=len(plain))
    return ([events.new_event(dest, op, ts=events.ts_str(start + timedelta(microseconds=k)), **fields)
             for k, (op, fields) in enumerate(plain)]
            + [events.new_event(dest, "trash", ts=events.ts_str(t), **fields) for t, fields in trash])


def _place(dest: Library, items: list[_Item], paths: dict[str, dict[str, str]], say: Say) -> None:
    """Copy every file to its final path; one already there with the right bytes is from an interrupted run."""
    jobs = [(it.old[f["role"]], dest.root / paths[it.id][f["role"]], f["sha256"]) for it in items for f in it.files]
    with HashCache(dest) as hashes:
        for src, dst, digest in _ticking(jobs, len(jobs), say, "copying"):
            if not (regular(dst) and hashes.sha(dst) == digest):
                fsops.copy_verified(src, dst, digest)
                hashes.record(dst, digest)


def _copy_cache_file(src: Path, dst: Path) -> bool:
    if regular(dst):  # placed whole by an interrupted run
        return True
    if not src.is_file():
        return False
    fsops.copy_verified(src, dst, file_sha256(src))
    return True


def _seed_cache(old_root: Path, dest: Library, items: list[_Item], say: Say) -> None:
    """Bring the prototype's thumbnails, previews and measurements along, so nothing is rendered again."""
    index, measured = Cache(dest), {}
    for it in _ticking(items, len(items), say, "copying thumbnails"):
        r = it.row
        got = {flag: int(bool(r[flag]) and _copy_cache_file(
                   old_root / ".library" / folder / f"{r['id'] % 100:02d}" / f"{r['id']}.jpg", target))
               for flag, folder, target in (("thumb", "thumbs", index.thumb_path(it.id)),
                                            ("preview", "previews", index.preview_path(it.id)))}
        if got["thumb"]:
            measured[it.id] = {c: r[c] for c in _MEASURED} | got
    index.seed_media(measured)


def _import_id(dest: Library, db_path: Path) -> str:
    # Named after the prototype's database, so a run after an interrupted one reuses its sources/ folder.
    tag = file_sha256(db_path)[:6]
    found = sorted(p.name for p in dest.sources.glob(f"*-icloud-{tag}"))
    return found[0] if found else f"{datetime.now():%Y-%m-%d}-icloud-{tag}"


def _write_sources(dest: Library, import_id: str, raw_dir: Path, items: list[_Item]) -> None:
    final = dest.sources / import_id
    if final.exists():
        return  # complete: an interrupted run moved it into place in one step
    stage = dest.cache_dir() / "migrate-v0"
    shutil.rmtree(stage, ignore_errors=True)  # an interrupted run's half-written folder
    stage.mkdir()  # fails if anything, a symlink say, is still there: never write through it
    for path in sorted(raw_dir.rglob("*.csv")):
        fsops.copy_verified(path, stage / path.relative_to(raw_dir), file_sha256(path))
    exif = raw_dir.parent / "exif.json"
    fsops.copy_verified(exif, stage / "exif.json", file_sha256(exif))
    manifest = {"import": import_id, "adapter": "icloud", "tool": f"photokeep {__version__} migrate-v0",
                "files": [s for it in items for s in it.sources]}
    with open(stage / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.rename(stage, final)


def _refuse_overlap(dest: Library, old_root: Path, raw_dir: Path) -> None:
    """The copy is written to and the prototype only read, so neither may lie inside the other."""
    new = dest.root.resolve()
    for other in (old_root.resolve(), raw_dir.resolve()):
        if new.is_relative_to(other) or other.is_relative_to(new):
            raise MigrationError(f"{dest.root} and {other} overlap: the copy must lie outside the prototype "
                                 "and its export")


def _summary(items: list[_Item], tombstones: dict[str, str], problems: list[dict]) -> dict:
    kinds = [kind for it in items for kind, _ in it.albums]
    return {"items": len(items), "trashed": sum(it.trashed for it in items),
            "live_clips": sum(len(it.files) > 1 for it in items), "tombstones": len(tombstones),
            "favorites": sum(it.favorite for it in items), "hidden": sum(it.hidden for it in items),
            "album_links": kinds.count("album"), "memory_links": kinds.count("memory"),
            "copied_bytes": sum(f["size"] for it in items for f in it.files), "problems": problems}


def migrate(old_root: Path, raw_dir: Path, zips: list[Path], dest: Library, dry_run: bool,
            progress: Say | None = None) -> dict:
    """Fill the new copy dest from the prototype at old_root; with dry_run, only check and count.

    Files are copied first and the log is written last, in one append, so an interrupted run is finished
    by running it again.
    """
    say = progress or (lambda _: None)
    db_path = old_root / ".library" / "library.db"
    _refuse_overlap(dest, old_root, raw_dir)
    with nullcontext() if dry_run else locked(dest):
        before = fold(events.read_all(dest))
        if before.items or before.tombstones:
            raise MigrationError(
                f"{dest.root} already has items; migrate-v0 only fills a new copy. If a migration into it was cut "
                f"off at the end, run `photokeep cache refresh {dest.root}` and `photokeep verify {dest.root}`; if "
                "verify fails, delete that copy's folder and migrate into a new one (the prototype is untouched)")
        rows = _rows(db_path)
        import_id = _import_id(dest, db_path)
        exif, members = _exif(raw_dir, import_id), zipscan.members(zips, skip=_NOT_MEDIA)
        hashed = _hash_files(sorted({old_root / r[c] for r in rows for c in ("path", "live") if r[c]}), say)
        problems: list[dict] = []
        items = _items(rows, old_root, hashed, exif, icloud.read(raw_dir), problems)
        known = {r["sha256"] for r in rows} | {h[0] for h in hashed.values() if h}
        tombstones = _tombstones(members, known, say, problems)
        if not dry_run:
            new = _events(dest, import_id, items, tombstones, datetime.now(UTC))
            _place(dest, items, assign(fold(events.ordered([*events.read_all(dest), *new]))), say)
            _seed_cache(old_root, dest, items, say)
            _write_sources(dest, import_id, raw_dir, items)
            events.append(dest, new)
            Cache(dest).refresh(render=False)
    return _summary(items, tombstones, problems)

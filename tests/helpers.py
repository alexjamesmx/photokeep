import hashlib
import io
import json
import os
import sqlite3
import uuid
import zipfile
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from photokeep import events, fsops, media
from photokeep.library import Library
from photokeep.paths import PART
from photokeep.state import State, fold

T0 = "2026-01-01T00:00:00.000000Z"
T1 = "2026-01-01T00:00:01.000000Z"
T2 = "2026-01-01T00:00:02.000000Z"
T3 = "2026-01-01T00:00:03.000000Z"
T4 = "2026-01-01T00:00:04.000000Z"
DETAILS_HEADER = "imgName,fileChecksum,favorite,hidden,deleted,originalCreationDate,viewCount\n"


def ev(op: str, item: str | None = None, *, ts: str = T1, copy: str = "c1", **fields) -> dict:
    e = {"v": 1, "id": uuid.uuid4().hex, "ts": ts, "copy": copy, "op": op, **fields}
    if item is not None:
        e["item"] = item
    return e


def imp(item: str, *, name: str = "IMG_1.JPG", folder: str = "2024/01", ts: str = T0,
        copy: str = "c1", fingerprint: list | None = None, live: str | None = None,
        live_sha: str | None = None, version_of: str | None = None, import_id: str | None = None,
        size: int = 1) -> dict:
    files = [{"role": "main", "name": name, "sha256": item, "size": size, "mtime": ts}]
    if live:
        files.append({"role": "live", "name": live, "sha256": live_sha or uuid.uuid4().hex, "size": 1, "mtime": ts})
    return ev("import", item, ts=ts, copy=copy, **{"import": import_id or uuid.uuid4().hex}, folder=folder,
              files=files, fingerprint=fingerprint, version_of=version_of)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def jpeg_bytes(color: tuple[int, int, int] = (200, 80, 40)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, "JPEG")
    return buf.getvalue()


def exif_jpeg(color: tuple[int, int, int] = (200, 80, 40), *, taken: str | None = "2024:03:05 10:00:00",
              model: str | None = "Pixel 7", size: tuple[int, int] = (8, 6)) -> bytes:
    """A JPEG whose EXIF holds a capture time and a camera, as exiftool reads them back."""
    from PIL import Image

    exif = Image.Exif()
    if model:
        exif[0x010F], exif[0x0110] = "Google", model  # Make, Model
    if taken:
        exif.get_ifd(0x8769)[0x9003] = taken  # DateTimeOriginal
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def put(lib: Library, rel: str, data: bytes) -> Path:
    path = lib.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def state_with(lib: Library, data: bytes, *, name: str = "IMG_1.JPG", folder: str = "2024/01",
               trashed: bool = False, purged: bool = False) -> State:
    """Log an import of `data` (optionally trashed, then purged) to lib's log and fold the whole log."""
    item = sha(data)
    evs = [imp(item, name=name, folder=folder, copy=lib.copy_id)]
    if trashed:
        evs.append(ev("trash", item, ts=T1, copy=lib.copy_id))
    if purged:
        evs.append(ev("purge", item, ts=T2, copy=lib.copy_id))
    events.append(lib, evs)
    return fold(events.read_all(lib))


def add_item(lib: Library, data: bytes, *, name: str = "IMG_1.JPG", folder: str = "2024/01", ts: str = T0,
             import_id: str | None = None) -> str:
    """Import `data` on lib as the importer would: file under originals/ plus an import event."""
    put(lib, f"originals/{folder}/{name}", data)
    item = sha(data)
    events.append(lib, [imp(item, name=name, folder=folder, ts=ts, copy=lib.copy_id, import_id=import_id,
                            size=len(data))])
    return item


def add_source(lib: Library, import_id: str, files: dict[str, bytes], *, exif: list[dict] = (),
               dates: dict[str, str] | None = None, adapter: str | None = "icloud") -> None:
    """Write sources/<import_id>/ as an iCloud import keeps it: manifest.json for `files` (source path -> bytes),
    exiftool's exif.json (run on raw/), and a part1 Photo Details CSV giving each name in `dates` its Apple-format
    date."""
    entries = [{"source_path": p, "sha256": sha(d), "size": len(d), "mtime": None} for p, d in files.items()]
    manifest = {"import": import_id, "exif_root": "raw", "files": entries} | ({"adapter": adapter} if adapter else {})
    put(lib, f"sources/{import_id}/manifest.json", json.dumps(manifest).encode())
    put(lib, f"sources/{import_id}/exif.json", json.dumps(list(exif)).encode())
    if dates:
        rows = "".join(f'{name},x,no,no,no,"{date}",0\n' for name, date in dates.items())
        put(lib, f"sources/{import_id}/part1/Photos/Photo Details.csv", (DETAILS_HEADER + rows).encode("utf-8-sig"))


_real_render = media.render  # taken at import, so worker processes and patched parents both get the real one


def render_or_die(item_id: str, path: Path, *args) -> dict:
    """media.render, except that a file named CRASH.JPG kills its worker process the way a segfault would."""
    if path.name == "CRASH.JPG":
        os._exit(1)
    return _real_render(item_id, path, *args)


def exit_worker(*_) -> None:
    """A worker task that dies the way an out-of-memory kill would."""
    os._exit(1)


def mark(lib: Library, op: str, item: str, ts: str = T1) -> None:
    events.append(lib, [ev(op, item, ts=ts, copy=lib.copy_id)])


def files_of(lib: Library) -> dict[str, bytes]:
    return {p.relative_to(lib.root).as_posix(): p.read_bytes()
            for top in (lib.originals, lib.trash) for p in sorted(top.rglob("*")) if p.is_file()}


def no_part_files(*libs: Library) -> bool:
    return not any(p.name.endswith(PART) for lib in libs for p in lib.root.rglob("*"))


def snapshot(*libs: Library) -> dict[str, bytes | None]:
    """Every file (bytes) and folder (None) of each copy, except the rebuildable cache/."""
    out: dict[str, bytes | None] = {}
    for n, lib in enumerate(libs):
        for p in sorted(lib.root.rglob("*")):
            rel = p.relative_to(lib.root)
            if rel.parts[0] != "cache":
                out[f"{n}/{rel.as_posix()}"] = None if p.is_dir() else p.read_bytes()
    return out


def fail_after(n: int) -> Callable[[Path, Path, str], None]:
    """A copy_verified that copies n files, then dies mid-copy like an unplugged drive."""
    real = fsops.copy_verified
    done: list[Path] = []

    def copy(src: Path, dst: Path, digest: str) -> None:
        if len(done) == n:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.with_name(dst.name + PART).write_bytes(src.read_bytes()[:1])
            raise fsops.CopyError(f"{dst}: device disappeared")
        real(src, dst, digest)
        done.append(dst)

    return copy


def tree(root: Path) -> dict[str, tuple[bytes, int] | None]:
    """Every file under root with its bytes and mtime, and every folder (None): proof that nothing was touched."""
    return {p.relative_to(root).as_posix(): None if p.is_dir() else (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*"))}


# The prototype's items table, verbatim from its library.py: migrate-v0 reads databases the prototype wrote.
V0_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL,            -- current location, relative to ROOT
  home TEXT NOT NULL,            -- organized location, where Restore puts it back
  live TEXT, live_home TEXT,     -- the short clip of a Live Photo
  kind TEXT NOT NULL,            -- photo | video
  screenshot INTEGER NOT NULL DEFAULT 0,
  ext TEXT NOT NULL,
  size INTEGER NOT NULL, live_size INTEGER NOT NULL DEFAULT 0,
  w INTEGER, h INTEGER, duration REAL, codec TEXT, live_codec TEXT,
  taken TEXT, tz TEXT, date_src TEXT,
  make TEXT, model TEXT, lat REAL, lon REAL,
  favorite INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0,
  albums TEXT NOT NULL DEFAULT '[]', memories TEXT NOT NULL DEFAULT '[]',
  orig TEXT, sha256 TEXT,
  sharp REAL, bright REAL, noise REAL, dhash TEXT,
  sim INTEGER, best INTEGER NOT NULL DEFAULT 0,
  thumb INTEGER NOT NULL DEFAULT 0, preview INTEGER NOT NULL DEFAULT 0,
  trashed INTEGER NOT NULL DEFAULT 0, trashed_at TEXT, reason TEXT
);
CREATE INDEX IF NOT EXISTS items_sha ON items(sha256);
"""


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


@dataclass
class V0:
    """A prototype library with the raw export folder and zip it was built from, as migrate-v0 finds them."""
    root: Path
    raw: Path
    zip: Path
    shas: dict[str, str]  # a, clip, b, c, extra
    trashed_at: str  # B's, naive local time as the prototype wrote it

    @property
    def db(self) -> Path:
        return self.root / ".library/library.db"

    def add(self, data: bytes | None, path: str, orig: str, *, clip: bytes | None = None, **cols) -> int:
        """A row and, unless data is None, its file at path (and clip at cols["live"], its size in live_size);
        other columns default."""
        for rel, content in ((path, data), (cols.get("live"), clip)):
            if rel and content is not None:
                _write(self.root / rel, content)
        row = {"path": path, "home": path, "kind": "photo", "ext": Path(path).suffix.lower(),
               "size": len(data or b""), "live_size": len(clip or b""), "orig": orig, "sha256": sha(data or b""),
               **cols}
        row |= {c: json.dumps(row[c]) for c in ("albums", "memories") if c in row}
        with closing(sqlite3.connect(self.db)) as db, db:
            return db.execute(f"INSERT INTO items ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                              list(row.values())).lastrowid

    def cache_file(self, folder: str, row_id: int, data: bytes) -> None:
        """A prototype thumbnail (folder "thumbs") or preview ("previews")."""
        _write(self.root / ".library" / folder / f"{row_id % 100:02d}" / f"{row_id}.jpg", data)


def make_v0(tmp_path: Path) -> V0:
    """Three JPEGs: A, a Live Photo (favorite, in album Trip and memory Summer); B, trashed yesterday; C, undated,
    hidden and in Trip. The zip holds them plus a photo the library no longer has, a CSV and a nested zip."""
    base = tmp_path / "v0"
    a, b, c, extra = (jpeg_bytes(rgb) for rgb in ((200, 80, 40), (40, 200, 80), (40, 80, 200), (90, 90, 90)))
    clip = b"live clip of A"
    trashed_at = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    v = V0(base / "Photos", base / "import" / "raw", base / "export.zip",
           {"a": sha(a), "clip": sha(clip), "b": sha(b), "c": sha(c), "extra": sha(extra)}, trashed_at)
    v.db.parent.mkdir(parents=True)
    with closing(sqlite3.connect(v.db)) as db:
        db.executescript(V0_SCHEMA)
    ida = v.add(a, "2024/2024-03/2024-03-05 10.00.00.jpg", "part1/IMG_0001.JPG", clip=clip,
                live="2024/2024-03/2024-03-05 10.00.00.mov", live_home="2024/2024-03/2024-03-05 10.00.00.mov",
                taken="2024-03-05T10:00:00", favorite=1, albums=["Trip"], memories=["Summer"],
                w=8, h=8, sharp=12.5, bright=100.0, noise=1.5, dhash="00ff00ff00ff00ff", thumb=1, preview=1)
    idb = v.add(b, ".library/trash/2.jpg", "part1/IMG_0002.JPG", home="2024/2024-03/2024-03-06 09.00.00.jpg",
                taken="2024-03-06T09:00:00", trashed=1, trashed_at=trashed_at, reason="Deleted in Photos", thumb=1)
    v.add(c, "Undated/IMG_0003.JPG", "part2/IMG_0003.JPG", hidden=1, albums=["Trip"])
    v.cache_file("thumbs", ida, b"thumb of A")
    v.cache_file("previews", ida, b"preview of A")
    v.cache_file("thumbs", idb, b"thumb of B")

    details = {"part1": ("IMG_0001.JPG,x,yes,no,no", "IMG_0001.mov,x,no,no,no", "IMG_0002.JPG,x,no,no,no"),
               "part2": ("IMG_0003.JPG,x,no,yes,no",)}
    for part, rows in details.items():
        text = DETAILS_HEADER + "".join(f'{r},"Tuesday March 5,2024 4:00 PM GMT",0\n' for r in rows)
        _write(v.raw / part / "Photos/Photo Details.csv", text.encode("utf-8-sig"))
    _write(v.raw / "part1/Albums/Trip.csv", b"Images\nIMG_0001.JPG\nIMG_0003.JPG\n")
    _write(v.raw / "part1/Memories/Summer.csv", b"Images\nIMG_0001.JPG\n")
    exif = [{"SourceFile": "raw/part1/Photos/IMG_0001.JPG", "ContentIdentifier": "CID-1", "MIMEType": "image/jpeg",
             "DateTimeOriginal": "2024:03:05 10:00:00", "Make": "Apple", "Model": "iPhone 15",
             "ImageWidth": 8, "ImageHeight": 8},
            {"SourceFile": "raw/part1/Photos/IMG_0001.mov", "ContentIdentifier": "CID-1",
             "MIMEType": "video/quicktime"},
            {"SourceFile": "raw/part1/Photos/IMG_0002.JPG", "MIMEType": "image/jpeg"},
            {"SourceFile": "raw/part2/Photos/IMG_0003.JPG", "MIMEType": "image/jpeg"},
            {"SourceFile": "raw/part1/Photos/Photo Details.csv", "MIMEType": "text/csv"}]
    _write(v.raw.parent / "exif.json", json.dumps(exif).encode())

    members = {"Photos/IMG_0001.JPG": a, "Photos/IMG_0001.mov": clip, "Photos/IMG_0002.JPG": b,
               "Photos/IMG_0003.JPG": c, "Recently Deleted/IMG_0099.JPG": extra,
               "Photos/Photo Details.csv": b"imgName\n", "nested.zip": b"not media"}
    top = "iCloud Photos Part 1 of 1"  # every export zip has one top folder
    with zipfile.ZipFile(v.zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.mkdir(top)
        for name, data in members.items():
            zf.writestr(f"{top}/{name}", data)
    return v

import hashlib
import uuid
from collections.abc import Callable
from pathlib import Path

from photokeep import events, fsops
from photokeep.library import Library
from photokeep.paths import PART
from photokeep.state import State, fold

T0 = "2026-01-01T00:00:00.000000Z"
T1 = "2026-01-01T00:00:01.000000Z"
T2 = "2026-01-01T00:00:02.000000Z"
T3 = "2026-01-01T00:00:03.000000Z"
T4 = "2026-01-01T00:00:04.000000Z"


def ev(op: str, item: str | None = None, *, ts: str = T1, copy: str = "c1", **fields) -> dict:
    e = {"v": 1, "id": uuid.uuid4().hex, "ts": ts, "copy": copy, "op": op, **fields}
    if item is not None:
        e["item"] = item
    return e


def imp(item: str, *, name: str = "IMG_1.JPG", folder: str = "2024/01", ts: str = T0,
        copy: str = "c1", fingerprint: list | None = None, live: str | None = None,
        version_of: str | None = None) -> dict:
    files = [{"role": "main", "name": name, "sha256": item, "size": 1, "mtime": ts}]
    if live:
        files.append({"role": "live", "name": live, "sha256": uuid.uuid4().hex, "size": 1, "mtime": ts})
    return ev("import", item, ts=ts, copy=copy, **{"import": uuid.uuid4().hex}, folder=folder,
              files=files, fingerprint=fingerprint, version_of=version_of)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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


def add_item(lib: Library, data: bytes, *, name: str = "IMG_1.JPG", folder: str = "2024/01", ts: str = T0) -> str:
    """Import `data` on lib as the importer would: file under originals/ plus an import event."""
    put(lib, f"originals/{folder}/{name}", data)
    item = sha(data)
    events.append(lib, [imp(item, name=name, folder=folder, ts=ts, copy=lib.copy_id)])
    return item


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

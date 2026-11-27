import hashlib
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from photokeep.hashcache import HashCache
from photokeep.library import Library
from photokeep.paths import PART, assign
from photokeep.state import State

_CHUNK = 1 << 20
# Exclusive create never follows a symlink planted at the temp path; O_BINARY stops Windows newline translation.
_NEW_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)


class CopyError(Exception):
    pass


def _refuse_existing(dst: Path) -> None:
    if os.path.lexists(dst):
        raise CopyError(f"{dst} already exists")


def copy_verified(src: Path, dst: Path, sha: str) -> None:
    """Copy src to dst through dst.part; dst appears only once the bytes match sha."""
    _refuse_existing(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + PART)
    part.unlink(missing_ok=True)  # left by an interrupted run; safe_name keeps originals off this name
    fd = os.open(part, _NEW_FILE, 0o666)
    try:
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as fout, open(src, "rb") as fin:
            while chunk := fin.read(_CHUNK):
                digest.update(chunk)
                fout.write(chunk)
            fout.flush()
            os.fsync(fout.fileno())
        if digest.hexdigest() != sha:
            raise CopyError(f"{src}: SHA-256 is {digest.hexdigest()}, expected {sha}")
        _refuse_existing(dst)  # a long copy leaves time for something else to create dst
        os.replace(part, dst)
    finally:
        part.unlink(missing_ok=True)


def _files(folder: Path) -> Iterator[Path]:
    # Regular files only: a symlink's bytes live elsewhere, and it may dangle.
    with os.scandir(folder) as entries:
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                yield from _files(Path(e.path))
            elif e.is_file(follow_symlinks=False):
                yield Path(e.path)


def _hash(cache: HashCache, path: Path, rehash: bool = False) -> str | None:
    try:
        return cache.sha(path, rehash)
    except FileNotFoundError:
        return None  # removed by another process since the walk


def scan(lib: Library, rehash: bool = False, cleanup: bool = True) -> dict[str, list[str]]:
    """Map the SHA-256 of every file under originals/ and trash/ to its sorted relative paths.

    Leftover copy_verified temp files are never reported; `cleanup` deletes them.
    """
    rels = sorted(p.relative_to(lib.root).as_posix() for top in (lib.originals, lib.trash) for p in _files(top))
    found: dict[str, list[str]] = {}
    with HashCache(lib) as cache:
        for rel in rels:
            if rel.endswith(PART):
                if cleanup:
                    (lib.root / rel).unlink(missing_ok=True)
            elif digest := _hash(cache, lib.root / rel, rehash):
                found.setdefault(digest, []).append(rel)
    return found


@dataclass
class LayoutPlan:
    moves: list[tuple[str, str]]  # (from, to), in an order where each destination is free
    deletes: list[str]
    missing: list[tuple[str, str]]  # (sha, expected path)
    untracked: list[str]
    conflicts: list[tuple[str, str, str]]  # (path, expected sha, found sha)
    delete_shas: dict[str, str] = field(default_factory=dict)  # what each delete held when planned

    def summary(self) -> dict[str, int]:
        return {"moves": len(self.moves), "deletes": len(self.deletes), "missing": len(self.missing),
                "untracked": len(self.untracked), "conflicts": len(self.conflicts)}


def _expected(state: State) -> list[tuple[str, str]]:
    out = []
    for item_id, roles in assign(state).items():
        shas = {f["role"]: f["sha256"] for f in state.items[item_id].files}
        out += [(rel, shas[role]) for role, rel in roles.items()]
    return sorted(out)


def plan_layout(lib: Library, state: State, found: dict[str, list[str]]) -> LayoutPlan:
    """Plan the moves and deletes that make `found` match the folded state, without touching disk."""
    kept = {f["sha256"] for i in state.items.values() if i.status != "purged" for f in i.files}
    purged = {f["sha256"] for i in state.items.values() if i.status == "purged" for f in i.files}
    # Only a purged item's own files are erased, never bytes a live or trashed item shares. A tombstone
    # has no import event here, so its bytes count as untracked and stay (spec 6.5).
    erasable, known = purged - kept, kept | purged
    at = {p: s for s, ps in found.items() for p in ps}
    deletes = sorted(p for p, s in at.items() if s in erasable)
    untracked = sorted(p for p, s in at.items() if s not in known)

    expected = _expected(state)
    placed = {rel for rel, s in expected if at.get(rel) == s}
    spare = {s: [p for p in ps if p not in placed] for s, ps in found.items()}
    todo: list[tuple[str, str, str | None]] = []  # (expected path, sha, source to move)
    for rel, s in expected:
        if rel not in placed:
            todo.append((rel, s, spare[s].pop(0) if spare.get(s) else None))

    # A move waits until an earlier move vacates its destination; whatever still waits is blocked.
    occupied = set(at) - set(deletes)
    moves: list[tuple[str, str]] = []
    waiting = [(src, rel) for rel, _, src in todo if src]
    while waiting:
        still = []
        for src, dst in waiting:
            if dst in occupied:
                still.append((src, dst))
            else:
                moves.append((src, dst))
                occupied.remove(src)
                occupied.add(dst)
        if len(still) == len(waiting):
            break
        waiting = still

    blocked = {dst for _, dst in waiting}
    missing, conflicts = [], []
    for rel, s, source in todo:
        if source and rel not in blocked:
            continue
        if rel in occupied:
            conflicts.append((rel, s, at[rel]))
        else:
            missing.append((s, rel))
    return LayoutPlan(moves, deletes, missing, untracked, conflicts, {p: at[p] for p in deletes})


def _remove_empty_folders(top: Path) -> None:
    for folder, _, _ in os.walk(top, topdown=False):
        if folder != os.fspath(top) and not os.listdir(folder):
            os.rmdir(folder)


def apply_layout(lib: Library, plan: LayoutPlan) -> None:
    """Carry out a plan; a step whose file changed since planning is skipped, never forced."""
    # Plan paths derive from other copies' logs: act only inside originals/ and trash/.
    tops = [lib.originals.resolve(), lib.trash.resolve()]

    def inside(path: Path) -> bool:
        return any(path.resolve().is_relative_to(top) for top in tops)

    with HashCache(lib) as cache:
        # Deletes first: a purged file may sit where a kept file has to move.
        for rel in plan.deletes:
            path, want = lib.root / rel, plan.delete_shas.get(rel)
            # The cache trusts size and mtime; re-read so a replaced file is never deleted.
            if want and inside(path) and path.is_file() and _hash(cache, path, rehash=True) == want:
                path.unlink(missing_ok=True)
                cache.forget(path)
        for src, dst in plan.moves:
            source, target = lib.root / src, lib.root / dst
            if inside(source) and inside(target) and source.is_file() and not os.path.lexists(target):
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)
                cache.move(source, target)
    for top in (lib.originals, lib.trash):
        _remove_empty_folders(top)

import errno
import hashlib
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from photokeep.hashcache import HashCache
from photokeep.library import Library
from photokeep.paths import PART, assign, os_junk
from photokeep.state import State

_CHUNK = 1 << 20
# Exclusive create never follows a symlink planted at the temp path; O_BINARY stops Windows newline translation.
_NEW_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)


# A full disk fails every file after it too, so it stops the run instead of being listed per file.
_DISK_FULL = frozenset({errno.ENOSPC, errno.EDQUOT})

Failed = list[tuple[str, str]]  # (path, reason): files a run could not write, left for the next run


class CopyError(Exception):
    pass


class HashMismatch(CopyError):
    """The source's bytes are not the ones expected: a bad source, not a failing destination."""


def failure(e: OSError | CopyError) -> str:
    """Why one file could not be written, so the run goes on with the rest; a full disk is raised again."""
    if isinstance(e, OSError) and e.errno in _DISK_FULL:
        raise e
    return str(e)


def _refuse_existing(dst: Path) -> None:
    if os.path.lexists(dst):
        raise CopyError(f"{dst} already exists")


def stage_stream(fin: BinaryIO, dst: Path) -> tuple[Path, str]:
    """Copy a stream to the temp file beside dst, flushed to disk; the temp file and the SHA-256 of what was
    written. The caller renames it into place."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + PART)
    part.unlink(missing_ok=True)  # left by an interrupted run; safe_name keeps originals off this name
    fd = os.open(part, _NEW_FILE, 0o666)
    try:
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as fout:
            while chunk := fin.read(_CHUNK):
                digest.update(chunk)
                fout.write(chunk)
            fout.flush()
            os.fsync(fout.fileno())
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return part, digest.hexdigest()


def stage_verified(src: Path, dst: Path, sha: str) -> Path:
    """Copy src to the temp file beside dst and check its bytes; the caller renames it into place."""
    with open(src, "rb") as fin:
        part, digest = stage_stream(fin, dst)
    if digest != sha:
        part.unlink(missing_ok=True)
        raise HashMismatch(f"{src}: SHA-256 is {digest}, expected {sha}")
    return part


def copy_verified(src: Path, dst: Path, sha: str) -> None:
    """Copy src to dst through a temp file beside it; dst appears only once the bytes match sha."""
    _refuse_existing(dst)
    part = stage_verified(src, dst, sha)
    try:
        _refuse_existing(dst)  # a long copy leaves time for something else to create dst
        os.replace(part, dst)
    finally:
        part.unlink(missing_ok=True)


def _files(folder: Path) -> Iterator[Path]:
    # Regular files only: a symlink's bytes live elsewhere, and it may dangle. OS junk is the system's
    # to keep, so it is never copied, moved, deleted or reported.
    with os.scandir(folder) as entries:
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                yield from _files(Path(e.path))
            elif e.is_file(follow_symlinks=False) and not os_junk(e.name):
                yield Path(e.path)


def _hash(cache: HashCache, path: Path, rehash: bool = False) -> str | None:
    try:
        return cache.sha(path, rehash)
    except FileNotFoundError:
        return None  # removed by another process since the walk


def scan(lib: Library, rehash: bool = False, cleanup: bool = True) -> dict[str, list[str]]:
    """Map the SHA-256 of every file under originals/ and trash/, OS junk aside, to its sorted relative paths.

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


def expected_files(state: State) -> list[tuple[str, str]]:
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

    expected = expected_files(state)
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
            try:
                os.rmdir(folder)
            except OSError:  # an empty folder holds nothing; one the disk will not remove is left
                pass


def inside(lib: Library, path: Path) -> bool:
    """True when path, with symlinks resolved, lies under lib's real originals/ or trash/."""
    # Paths derive from other copies' logs and folders: file changes act only inside these two.
    # The two are not resolved themselves, so a symlinked originals/ or trash/ counts as outside.
    root = lib.root.resolve()
    return any(path.resolve().is_relative_to(root / top) for top in ("originals", "trash"))


def overlap(a: Path, b: Path) -> bool:
    """True when a and b, symlinks resolved, are one folder or one lies inside the other."""
    a, b = a.resolve(), b.resolve()
    return a.is_relative_to(b) or b.is_relative_to(a)


def _delete(lib: Library, cache: HashCache, rel: str, want: str | None) -> None:
    path = lib.root / rel
    # The cache trusts size and mtime; re-read so a replaced file is never deleted.
    if want and inside(lib, path) and path.is_file() and _hash(cache, path, rehash=True) == want:
        path.unlink(missing_ok=True)
        cache.forget(path)


def _move(lib: Library, cache: HashCache, src: str, dst: str) -> None:
    source, target = lib.root / src, lib.root / dst
    if inside(lib, source) and inside(lib, target) and source.is_file() and not os.path.lexists(target):
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
        cache.move(source, target)


def apply_layout(lib: Library, plan: LayoutPlan) -> Failed:
    """Carry out a plan and return the steps the disk refused; a step whose file changed since planning
    is skipped, never forced."""
    failed: Failed = []
    with HashCache(lib) as cache:
        # Deletes first: a purged file may sit where a kept file has to move.
        for rel in plan.deletes:
            try:
                _delete(lib, cache, rel, plan.delete_shas.get(rel))
            except OSError as e:
                failed.append((rel, failure(e)))
        for src, dst in plan.moves:
            try:
                _move(lib, cache, src, dst)
            except OSError as e:
                failed.append((src, failure(e)))
    for top in (lib.originals, lib.trash):
        if not top.is_symlink():  # its empty folders would be someone else's
            _remove_empty_folders(top)
    return failed

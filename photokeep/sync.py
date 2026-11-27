"""Two-way sync of two copies of one library: logs, then files, then the app."""
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from typing import NamedTuple

from photokeep import events, fsops
from photokeep.events import LogCorrupt, encode, new_event, ordered, parse_log, read_all, ts_str
from photokeep.fsops import LayoutPlan, apply_layout, inside, plan_layout, scan
from photokeep.hashcache import HashCache
from photokeep.library import Library, LibraryError, locked
from photokeep.state import State, fold

_VERSION = re.compile(r"""^__version__\s*=\s*["']([0-9]+(?:\.[0-9]+)*)["']""", re.MULTILINE)

Found = dict[str, list[str]]  # what scan returns: sha -> paths
Files = list[tuple[str, str]]  # (sha, expected path)


@dataclass
class SyncPlan:
    logs: list[tuple[str, str, int]]  # (log file, "a->b" | "b->a", bytes)
    new_purges: list[str]  # item ids whose 30 days in the trash are over
    copy_a_to_b: Files  # filled on b from a
    copy_b_to_a: Files
    layout_a: LayoutPlan
    layout_b: LayoutPlan
    app: str | None  # which way app/ is copied, if at all
    missing_both: Files = field(default_factory=list)  # expected on both, held by neither
    pending: tuple[LayoutPlan, LayoutPlan] | None = None  # after a run: what a fresh plan of each side still finds

    def summary(self) -> dict:
        # Conflicts are counted where they stand last: after a run, in the fresh plans.
        final = self.pending or (self.layout_a, self.layout_b)
        return {
            "logs": [list(t) for t in self.logs],
            "new_purges": len(self.new_purges),
            "copy_a_to_b": len(self.copy_a_to_b),
            "copy_b_to_a": len(self.copy_b_to_a),
            "layout_a": self.layout_a.summary(),
            "layout_b": self.layout_b.summary(),
            "missing_both": len(self.missing_both),
            "app": self.app,
            "conflicts": sum(len(p.conflicts) for p in final),
            "pending": {"a": self.pending[0].summary(), "b": self.pending[1].summary()} if self.pending else None,
        }


class _Log(NamedTuple):
    name: str
    on_a: bytes  # complete lines only
    on_b: bytes


def _check(a: Library, b: Library) -> None:
    if a.library_id != b.library_id:
        raise LibraryError(f"{a.root} and {b.root} are copies of different libraries")
    if a.copy_id == b.copy_id:
        raise LibraryError(f"{a.root} and {b.root} are the same copy")


def _complete_lines(lib: Library, name: str) -> bytes:
    path = lib.log_dir / name
    # A symlinked log would let another drive read or extend a file outside the library.
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise LogCorrupt(f"{path} is not a regular file")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return b""
    return raw[:raw.rfind(b"\n") + 1]  # a torn final line was never written as far as the log is concerned


def _compare(a: Library, b: Library) -> list[_Log]:
    names = sorted({p.name for lib in (a, b) for p in lib.log_dir.glob("*.jsonl")})
    logs = [_Log(name, _complete_lines(a, name), _complete_lines(b, name)) for name in names]
    for log in logs:
        # Logs only grow, so the shorter must be a prefix of the longer; anything else is never merged.
        if not (log.on_a.startswith(log.on_b) or log.on_b.startswith(log.on_a)):
            raise LogCorrupt(f"{log.name} differs between {a.root} and {b.root}; diverged logs are never merged")
    return logs


class _Append(NamedTuple):
    dst: Library
    name: str
    at: int  # bytes dst's log holds now
    tail: bytes
    direction: str

    def report(self) -> tuple[str, str, int]:
        return self.name, self.direction, len(self.tail)


def _appends(a: Library, b: Library, logs: list[_Log]) -> list[_Append]:
    todo = []
    for log in logs:
        if len(log.on_a) == len(log.on_b):
            continue
        src, dst, longer, shorter, direction = (
            (a, b, log.on_a, log.on_b, "a->b") if len(log.on_a) > len(log.on_b) else (b, a, log.on_b, log.on_a, "b->a"))
        parse_log(longer, str(src.log_dir / log.name))  # never spread a line no reader could fold
        todo.append(_Append(dst, log.name, len(shorter), longer[len(shorter):], direction))
    return todo


def exchange_logs(a: Library, b: Library, dry_run: bool) -> list[tuple[str, str, int]]:
    """Append to each copy the log lines only the other has; return (file, direction, bytes) per log copied.

    Every log is compared and every line to copy is validated before the first write, so a diverged
    or corrupt log changes nothing on either copy.
    """
    todo = _appends(a, b, _compare(a, b))
    if not dry_run:
        for t in todo:
            events.append_lines(t.dst.log_dir / t.name, t.tail, at=t.at)
    return [t.report() for t in todo]


def _purges(a: Library, state: State, now: datetime) -> list[dict]:
    return [new_event(a, "purge", ts=ts_str(now), item=item) for item in state.due_purges(now)]


def _merged(logs: list[tuple[str, str, int]]) -> list[tuple[str, str, int]]:
    total: dict[tuple[str, str], int] = {}
    for name, direction, size in logs:
        total[name, direction] = total.get((name, direction), 0) + size
    return sorted((name, direction, size) for (name, direction), size in total.items())


def _fills(layouts: tuple[LayoutPlan, LayoutPlan], found: tuple[Found, Found]) -> tuple[Files, Files, Files]:
    """Split each side's missing files into those the other side holds (any path) and those neither has."""
    (layout_a, layout_b), (found_a, found_b) = layouts, found
    a_to_b = [m for m in layout_b.missing if m[0] in found_a]
    b_to_a = [m for m in layout_a.missing if m[0] in found_b]
    neither = sorted({m for layout, other in ((layout_a, found_b), (layout_b, found_a))
                      for m in layout.missing if m[0] not in other})
    return a_to_b, b_to_a, neither


def _app_version(lib: Library) -> tuple[int, ...] | None:
    try:
        text = (lib.app / "photokeep" / "__init__.py").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    m = _VERSION.search(text)
    return tuple(int(n) for n in m.group(1).split(".")) if m else None


def _app_direction(a: Library, b: Library) -> str | None:
    va, vb = _app_version(a), _app_version(b)
    if va is None or vb is None or va == vb:
        return None
    return "a->b" if va > vb else "b->a"


def plan(a: Library, b: Library, now: datetime) -> SyncPlan:
    """What run would do, worked out without writing to either copy (the rebuildable hash cache aside)."""
    _check(a, b)
    logs = _compare(a, b)
    transfers = [t.report() for t in _appends(a, b, logs)]
    union = [e for log in logs for e in parse_log(max(log.on_a, log.on_b, key=len), log.name)]
    purges = _purges(a, fold(ordered(union)), now)
    state = fold(ordered(union + purges))
    if purges:
        transfers.append((a.log_path().name, "a->b", len(encode(purges))))
    found = scan(a, cleanup=False), scan(b, cleanup=False)
    layouts = plan_layout(a, state, found[0]), plan_layout(b, state, found[1])
    a_to_b, b_to_a, neither = _fills(layouts, found)
    return SyncPlan(_merged(transfers), [e["item"] for e in purges], a_to_b, b_to_a, *layouts,
                    _app_direction(a, b), neither)


def _arrange(lib: Library, state: State) -> LayoutPlan:
    layout = plan_layout(lib, state, scan(lib))
    apply_layout(lib, layout)
    return layout


def _copy(src: Library, dst: Library, found: Found, todo: Files) -> None:
    with HashCache(dst) as cache:
        for sha, rel in todo:
            target = dst.root / rel
            if inside(dst, target):
                fsops.copy_verified(src.root / found[sha][0], target, sha)
                cache.record(target, sha)  # verified while copying: the next scan need not read it again


def _replace_app(src: Library, dst: Library) -> None:
    work = dst.cache / "app-update"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(src.app, work / "new", symlinks=True)
    # Two renames: a crash between them leaves no app/, which _restore_app puts back on the next run.
    os.replace(dst.app, work / "old")
    os.replace(work / "new", dst.app)
    shutil.rmtree(work)


def _restore_app(lib: Library) -> None:
    old = lib.cache / "app-update" / "old"
    if not lib.app.exists() and old.is_dir():
        os.replace(old, lib.app)


def after_sync(lib: Library) -> None:
    """Refresh a synced copy's derived caches; none exist yet beyond the hash cache, which scan keeps current."""


def run(a: Library, b: Library, now: datetime) -> SyncPlan:
    """Sync both copies both ways. Every step is idempotent, so an interrupted run is finished by running again."""
    _check(a, b)
    with locked(a), locked(b):
        transfers = exchange_logs(a, b, dry_run=False)
        purges = _purges(a, fold(read_all(a)), now)
        events.append(a, purges)
        transfers += exchange_logs(a, b, dry_run=False)
        state = fold(read_all(a))
        # Arrange before copying: a move on one side can free the path a copied file has to take.
        layouts = _arrange(a, state), _arrange(b, state)
        found = scan(a), scan(b)
        a_to_b, b_to_a, neither = _fills((plan_layout(a, state, found[0]), plan_layout(b, state, found[1])), found)
        _copy(a, b, found[0], a_to_b)
        _copy(b, a, found[1], b_to_a)
        # apply_layout skips steps whose files changed meanwhile; fresh plans show what is left.
        pending = plan_layout(a, state, scan(a)), plan_layout(b, state, scan(b))
        for lib in (a, b):
            _restore_app(lib)
        app = _app_direction(a, b)
        if app:
            _replace_app(*((a, b) if app == "a->b" else (b, a)))
        for lib in (a, b):
            after_sync(lib)
    return SyncPlan(_merged(transfers), [e["item"] for e in purges], a_to_b, b_to_a, *layouts, app, neither, pending)

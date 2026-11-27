"""Two-way sync of two copies of one library: logs, then files, then (only when asked) the app."""
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from photokeep import events, fsops
from photokeep.events import LogCorrupt, encode, new_event, ordered, parse_log, read_all, ts_str
from photokeep.fsops import LayoutPlan, apply_layout, inside, plan_layout, scan
from photokeep.hashcache import HashCache
from photokeep.library import Library, LibraryError, locked
from photokeep.state import State, fold

_VERSION = re.compile(r"""^__version__\s*=\s*["']([0-9]+(?:\.[0-9]+)*)["']""", re.MULTILINE)
_APP_WORK = "app-update-"  # prefix of the app update's work folders under cache/

Found = dict[str, list[str]]  # what scan returns: sha -> paths
Files = list[tuple[str, str]]  # (sha, expected path)


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in version.split("."))


@dataclass(frozen=True)
class AppUpdate:
    a: str | None  # __version__ of each copy's app/, None when it has none
    b: str | None

    @property
    def direction(self) -> str | None:
        """Which way the newer app would be copied; None when either copy has none or both match."""
        if self.a is None or self.b is None or _version_key(self.a) == _version_key(self.b):
            return None
        return "a->b" if _version_key(self.a) > _version_key(self.b) else "b->a"

    def to_json(self) -> dict:
        return {"direction": self.direction, "a": self.a, "b": self.b}


@dataclass
class SyncPlan:
    logs: list[tuple[str, str, int]]  # (log file, "a->b" | "b->a", bytes)
    new_purges: list[tuple[str, str | None]]  # (item, trashed at) for items whose 30 days in the trash are over
    copy_a_to_b: Files  # filled on b from a
    copy_b_to_a: Files
    layout_a: LayoutPlan
    layout_b: LayoutPlan
    app: AppUpdate  # copied only by run(update_app=True)
    missing_both: Files = field(default_factory=list)  # expected on both, held by neither
    pending: tuple[LayoutPlan, LayoutPlan] | None = None  # after a run: what a fresh plan of each side still finds
    bad_sources: list[tuple[str, str, str]] = field(default_factory=list)  # (side, path, expected sha), skipped

    def summary(self) -> dict:
        # Conflicts are counted where they stand last: after a run, in the fresh plans.
        final = self.pending or (self.layout_a, self.layout_b)
        return {
            "logs": [list(t) for t in self.logs],
            "new_purges": [{"item": item, "trashed_at": ts} for item, ts in self.new_purges],
            "copy_a_to_b": len(self.copy_a_to_b),
            "copy_b_to_a": len(self.copy_b_to_a),
            "layout_a": self.layout_a.summary(),
            "layout_b": self.layout_b.summary(),
            "missing_both": len(self.missing_both),
            "app": self.app.to_json(),
            "conflicts": sum(len(p.conflicts) for p in final),
            "bad_sources": [{"side": side, "path": path, "sha": sha} for side, path, sha in self.bad_sources],
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
        parse_log(longer, str(src.log_dir / log.name))  # raises on a malformed line before anything is written
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


def _due(state: State, now: datetime) -> list[tuple[str, str | None]]:
    return [(item, state.items[item].trashed_at) for item in state.due_purges(now)]


def _purge_events(a: Library, due: list[tuple[str, str | None]], now: datetime) -> list[dict]:
    return [new_event(a, "purge", ts=ts_str(now), item=item) for item, _ in due]


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


def _app_version(lib: Library) -> str | None:
    try:
        text = (lib.app / "photokeep" / "__init__.py").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    m = _VERSION.search(text)
    return m.group(1) if m else None


def _app(a: Library, b: Library) -> AppUpdate:
    return AppUpdate(_app_version(a), _app_version(b))


def plan(a: Library, b: Library, now: datetime) -> SyncPlan:
    """What run would do, worked out without writing to either copy (the rebuildable hash cache aside)."""
    _check(a, b)
    logs = _compare(a, b)
    transfers = [t.report() for t in _appends(a, b, logs)]
    union = [e for log in logs for e in parse_log(max(log.on_a, log.on_b, key=len), log.name)]
    due = _due(fold(ordered(union)), now)
    purges = _purge_events(a, due, now)
    state = fold(ordered(union + purges))
    if purges:
        transfers.append((a.log_path().name, "a->b", len(encode(purges))))
    found = scan(a, cleanup=False), scan(b, cleanup=False)
    layouts = plan_layout(a, state, found[0]), plan_layout(b, state, found[1])
    a_to_b, b_to_a, neither = _fills(layouts, found)
    return SyncPlan(_merged(transfers), due, a_to_b, b_to_a, *layouts, _app(a, b), neither)


def _arrange(lib: Library, state: State) -> LayoutPlan:
    layout = plan_layout(lib, state, scan(lib))
    apply_layout(lib, layout)
    return layout


def _copy(src: Library, dst: Library, side: str, found: Found, todo: Files) -> list[tuple[str, str, str]]:
    """Fill dst's missing files from src; return the sources skipped because their bytes are not the expected ones."""
    bad = []
    with HashCache(dst) as cache:
        for sha, rel in todo:
            source, target = found[sha][0], dst.root / rel
            if not inside(dst, target):
                continue
            try:
                fsops.copy_verified(src.root / source, target, sha)
            except fsops.HashMismatch:  # rotted source: the rest is still worth copying
                bad.append((side, source, sha))
                continue
            cache.record(target, sha)  # verified while copying: the next scan need not read it again
    return bad


def _refuse_links(top: Path) -> None:
    """Raise unless top holds only plain folders and files: a symlink in an app could run code from anywhere."""
    paths = [top] + [Path(folder) / name for folder, dirs, files in os.walk(top) for name in dirs + files]
    for path in paths:
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise LibraryError(f"{path} is a symlink or a special file; the app is not copied")


def _replace_app(src: Library, dst: Library) -> None:
    work = Path(tempfile.mkdtemp(prefix=_APP_WORK, dir=dst.cache_dir()))
    try:
        shutil.copytree(src.app, work / "new", symlinks=True)
        _refuse_links(work / "new")  # src may have changed since it was checked
    except BaseException:
        shutil.rmtree(work)
        raise
    os.replace(dst.app, work / "old")
    os.replace(work / "new", dst.app)  # a crash between the two renames leaves old/ for _restore_app
    shutil.rmtree(work)


def _restore_app(lib: Library) -> None:
    """Clear up after an app update that was cut off: put app/ back if it is gone, then drop the work folders."""
    for work in sorted(lib.cache_dir().glob(_APP_WORK + "*")):
        if work.is_symlink() or not work.is_dir():
            continue
        old = work / "old"
        if not os.path.lexists(lib.app) and old.is_dir() and not old.is_symlink():
            os.replace(old, lib.app)
        shutil.rmtree(work)


def after_sync(lib: Library) -> None:
    """Refresh a synced copy's derived caches; none exist yet beyond the hash cache, which scan keeps current."""


def run(a: Library, b: Library, now: datetime, update_app: bool = False) -> SyncPlan:
    """Sync both copies both ways; with update_app, also copy the newer app/ over the older one.

    Every step is idempotent, so an interrupted run is finished by running it again.
    """
    _check(a, b)
    with locked(a), locked(b):
        if update_app:
            for lib in (a, b):
                _restore_app(lib)
        app = _app(a, b)
        ends = None
        if update_app and app.direction:
            ends = (a, b) if app.direction == "a->b" else (b, a)
            _refuse_links(ends[0].app)  # refused before anything changes
        transfers = exchange_logs(a, b, dry_run=False)
        due = _due(fold(read_all(a)), now)
        events.append(a, _purge_events(a, due, now))
        transfers += exchange_logs(a, b, dry_run=False)
        state = fold(read_all(a))
        # Arrange before copying: a move on one side can free the path a copied file has to take.
        layouts = _arrange(a, state), _arrange(b, state)
        found = scan(a), scan(b)
        a_to_b, b_to_a, neither = _fills((plan_layout(a, state, found[0]), plan_layout(b, state, found[1])), found)
        bad = _copy(a, b, "a", found[0], a_to_b) + _copy(b, a, "b", found[1], b_to_a)
        # apply_layout skips steps whose files changed meanwhile; fresh plans show what is left.
        pending = plan_layout(a, state, scan(a)), plan_layout(b, state, scan(b))
        if ends:
            _replace_app(*ends)
        for lib in (a, b):
            after_sync(lib)
    return SyncPlan(_merged(transfers), due, a_to_b, b_to_a, *layouts, app, neither, pending, bad)

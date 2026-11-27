import json
import os
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

try:
    import fcntl
except ImportError:  # Windows: appends are unlocked, best effort
    fcntl = None

if TYPE_CHECKING:
    from photokeep.library import Library

OPS = frozenset({
    "copy.name", "import", "favorite", "unfavorite", "hide", "unhide",
    "album.add", "album.remove", "album.rename", "trash", "restore", "purge", "readmit",
})
_CHUNK = 8192


class LogCorrupt(Exception):
    pass


def ts_str(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def utcnow() -> str:
    return ts_str(datetime.now(UTC))


def new_event(lib: "Library", op: str, *, ts: str | None = None, **fields) -> dict:
    # Unknown ops are accepted so a newer copy's events survive in older ones.
    return {"v": 1, "id": uuid.uuid4().hex, "ts": ts or utcnow(), "copy": lib.copy_id, "op": op, **fields}


def _drop_torn_tail(fd: int) -> None:
    size = os.fstat(fd).st_size
    if size == 0 or os.pread(fd, 1, size - 1) == b"\n":
        return
    # A crash left a partial line. It was never acknowledged, so cut it back to the last full line.
    end = size
    while end > 0:
        start = max(0, end - _CHUNK)
        cut = os.pread(fd, end - start, start).rfind(b"\n")
        if cut >= 0:
            os.ftruncate(fd, start + cut + 1)
            return
        end = start
    os.ftruncate(fd, 0)


def encode(events: Iterable[dict]) -> bytes:
    return "".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n" for e in events).encode()


def append_lines(path: Path, data: bytes, at: int | None = None) -> None:
    """Append whole lines to a log under its lock, after cutting a torn final line.

    With `at`, the log must then hold exactly `at` bytes: lines copied from another copy's log land
    right after the bytes they were compared against, or not at all.
    """
    if not data:
        return
    # A log on another drive may be a planted symlink; appending through it would write outside the library.
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX)
        _drop_torn_tail(fd)
        if at is not None and os.fstat(fd).st_size != at:
            raise LogCorrupt(f"{path.name} changed while sync was copying it; run sync again")
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)  # also releases the lock


def append(lib: "Library", events: Iterable[dict]) -> None:
    append_lines(lib.log_path(), encode(events))


def parse_log(raw: bytes, name: str) -> list[dict]:
    """The events in a log's bytes; a torn final line (crash mid-append) is not part of the log."""
    *lines, _torn = raw.split(b"\n")
    events = []
    for n, line in enumerate(lines, 1):
        try:
            e = json.loads(line)
        except ValueError:
            e = None
        # Every reader sorts and folds by these keys; an event without them must stop here, not spread.
        if not isinstance(e, dict) or not all(isinstance(e.get(k), str) for k in ("ts", "copy", "id", "op")):
            raise LogCorrupt(f"{name}: line {n} is not a valid event")
        events.append(e)
    return events


def read_log(path: Path) -> list[dict]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    return parse_log(raw, path.name)


def ordered(events: Iterable[dict]) -> list[dict]:
    return sorted(events, key=lambda e: (e["ts"], e["copy"], e["id"]))


def read_all(lib: "Library") -> list[dict]:
    return ordered(e for p in sorted(lib.log_dir.glob("*.jsonl")) for e in read_log(p))

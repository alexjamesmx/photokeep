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


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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


def append(lib: "Library", events: Iterable[dict]) -> None:
    data = "".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n" for e in events).encode()
    if not data:
        return
    fd = os.open(lib.log_path(), os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX)
        _drop_torn_tail(fd)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)  # also releases the lock


def read_log(path: Path) -> list[dict]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    *lines, _torn = raw.split(b"\n")
    events = []
    for n, line in enumerate(lines, 1):
        try:
            e = json.loads(line)
        except ValueError:
            e = None
        if not isinstance(e, dict):
            raise LogCorrupt(f"{path.name}: line {n} is not a valid event")
        events.append(e)
    return events


def read_all(lib: "Library") -> list[dict]:
    events = [e for p in sorted(lib.log_dir.glob("*.jsonl")) for e in read_log(p)]
    return sorted(events, key=lambda e: (e["ts"], e["copy"], e["id"]))

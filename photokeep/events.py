import errno
import json
import os
import re
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from photokeep import locks

if TYPE_CHECKING:
    from photokeep.library import Library

# The string fields fold reads from each known op; a line without them would break every copy it reaches.
_FIELDS: dict[str, tuple[str, ...]] = {
    "copy.name": ("name",),
    "import": ("item", "folder", "import"),
    **dict.fromkeys(("favorite", "unfavorite", "hide", "unhide", "trash", "restore", "purge", "readmit"), ("item",)),
    **dict.fromkeys(("album.add", "album.remove"), ("item", "kind", "album")),
    "album.rename": ("kind", "album", "to"),
}
OPS = frozenset(_FIELDS)
_KEYS = ("ts", "copy", "id", "op")  # every reader sorts and folds by these
_FILE_KEYS = ("role", "name", "sha256")
_LINKS = ("version_of", "edit_of")  # optional ids of the item an import is another version, or an edit, of
_ID = re.compile(r"[0-9a-f]{64}")
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


def _write_all(fd: int, data: bytes, path: Path) -> None:
    # A write may take only part of the bytes (a disk filling up); the rest follows, or the append fails.
    rest = memoryview(data)
    while rest:
        n = os.write(fd, rest)
        if n == 0:
            raise OSError(errno.EIO, f"{path.name}: the disk took no bytes of the append")
        rest = rest[n:]


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
        locks.flock(fd)
        _drop_torn_tail(fd)
        start = os.fstat(fd).st_size
        if at is not None and start != at:
            raise LogCorrupt(f"{path.name} changed while sync was copying it; run sync again")
        _write_all(fd, data, path)
        if os.fstat(fd).st_size != start + len(data):
            raise LogCorrupt(f"{path.name} does not hold the lines just appended; run sync again")
        os.fsync(fd)
    finally:
        os.close(fd)  # also releases the lock


def append(lib: "Library", events: Iterable[dict]) -> None:
    append_lines(lib.log_path(), encode(events))


def _strings(d: dict, keys: Iterable[str]) -> bool:
    return all(isinstance(d.get(k), str) for k in keys)


def _valid_file(f: object) -> bool:
    if not isinstance(f, dict) or not _strings(f, _FILE_KEYS):
        return False
    size = f.get("size")
    return isinstance(size, int) and not isinstance(size, bool) and size >= 0


def _valid_import(e: dict) -> bool:
    files, fingerprint = e.get("files"), e.get("fingerprint")
    if not (isinstance(files, list) and all(_valid_file(f) for f in files)):
        return False
    # The item is its main file: layout, verify and status read each file's role, hash and size.
    mains = [f for f in files if f["role"] == "main"]
    return (_ID.fullmatch(e["item"]) is not None and len(mains) == 1 and mains[0]["sha256"] == e["item"]
            # fold turns the fingerprint into a dict key, so it must hold plain values only
            and (fingerprint is None or isinstance(fingerprint, list)
                 and not any(isinstance(x, list | dict) for x in fingerprint))
            and all(e.get(k) is None or isinstance(e[k], str) and _ID.fullmatch(e[k]) is not None for k in _LINKS))


def _valid_fields(e: dict) -> bool:
    # fold looks up every event's item, known op or not, so it must be hashable
    if "item" in e and not isinstance(e["item"], str):
        return False
    return _strings(e, _FIELDS.get(e["op"], ())) and (e["op"] != "import" or _valid_import(e))


def parse_log(raw: bytes, name: str) -> list[dict]:
    """The events in a log's bytes; a torn final line (crash mid-append) is not part of the log."""
    *lines, _torn = raw.split(b"\n")
    events = []
    for n, line in enumerate(lines, 1):
        try:
            e = json.loads(line)
        except ValueError:
            e = None
        if not isinstance(e, dict) or not _strings(e, _KEYS):
            raise LogCorrupt(f"{name}: line {n} is not a valid event")
        if not _valid_fields(e):
            raise LogCorrupt(f"{name}: line {n} is not a valid {e['op']} event")
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

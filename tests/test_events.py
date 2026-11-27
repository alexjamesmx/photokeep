import errno
import json
import multiprocessing
import os
import re

import pytest

from photokeep.events import LogCorrupt, append, new_event, parse_log, read_all, read_log
from photokeep.library import init_copy, init_library, open_library


def _append_many(root, n):
    lib = open_library(root)
    for _ in range(n):
        append(lib, [new_event(lib, "favorite", item="f" * 64)])


def run_parallel_appends(lib, procs, n):
    ctx = multiprocessing.get_context("spawn")
    workers = [ctx.Process(target=_append_many, args=(lib.root, n)) for _ in range(procs)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
        assert w.exitcode == 0


def test_append_and_read_roundtrip(lib):
    append(lib, [new_event(lib, "favorite", item="a" * 64)])
    [e] = [x for x in read_log(lib.log_path()) if x["op"] == "favorite"]
    assert e["v"] == 1 and e["copy"] == lib.copy_id and e["ts"].endswith("Z")


def test_torn_last_line_is_ignored_and_next_append_starts_fresh(lib):
    p = lib.log_path()
    with open(p, "ab") as f:
        f.write(b'{"v":1,"id":"x","op":"fav')
    assert all(e["op"] != "fav" for e in read_log(p))
    append(lib, [new_event(lib, "hide", item="b" * 64)])
    assert read_log(p)[-1]["op"] == "hide"


def test_bad_line_in_middle_raises(lib):
    p = lib.log_path()
    p.write_text('{"v":1}\nnot json\n{"v":1}\n')
    with pytest.raises(LogCorrupt, match=str(p.name)):
        read_log(p)


def test_concurrent_appends_keep_whole_lines(lib):
    run_parallel_appends(lib, procs=4, n=200)
    assert len([e for e in read_log(lib.log_path()) if e["op"] == "favorite"]) == 800


def test_init_writes_copy_name_event(tmp_path):
    lib = init_library(tmp_path / "L", "laptop")
    assert read_log(lib.log_path())[0] | {"id": None, "ts": None} == {
        "v": 1, "id": None, "ts": None, "copy": lib.copy_id, "op": "copy.name", "name": "laptop",
    }


def test_unknown_op_survives_read(lib):
    append(lib, [new_event(lib, "future.thing", item="c" * 64)])
    assert read_log(lib.log_path())[-1]["op"] == "future.thing"


def test_read_all_orders_by_ts_copy_id_across_copies(lib, tmp_path):
    other = init_copy(tmp_path / "M", "usb", lib)
    (lib.log_dir / f"{other.copy_id}.jsonl").write_bytes(other.log_path().read_bytes())
    append(lib, [new_event(lib, "hide", ts="2000-01-01T00:00:00.000000Z", item="d" * 64)])
    events = read_all(lib)
    keys = [(e["ts"], e["copy"], e["id"]) for e in events]
    assert keys == sorted(keys)
    assert events[0]["op"] == "hide" and {e["copy"] for e in events} == {lib.copy_id, other.copy_id}


def test_read_log_missing_file_is_empty(lib, tmp_path):
    assert read_log(tmp_path / "nope.jsonl") == []


@pytest.mark.parametrize("code", [errno.ENOTSUP, errno.ENOLCK, errno.EINVAL])
def test_append_works_where_the_filesystem_has_no_locks(lib, monkeypatch, code):
    fcntl = pytest.importorskip("fcntl")

    def unsupported(fd, flags):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(fcntl, "flock", unsupported)
    append(lib, [new_event(lib, "favorite", item="e" * 64)])
    assert read_log(lib.log_path())[-1]["op"] == "favorite"


ITEM = "a" * 64
FILE = {"role": "main", "name": "A.JPG", "sha256": ITEM, "size": 5}
CLIP = {"role": "live", "name": "A.MOV", "sha256": "b" * 64, "size": 3}
IMPORT = {"op": "import", "item": ITEM, "folder": "2024/01", "import": "i", "files": [FILE], "fingerprint": None}


@pytest.mark.parametrize("fields", [
    {"op": "future.thing"},
    {"op": "copy.name", "name": "drive"},
    IMPORT,
    IMPORT | {"fingerprint": ["2024-01-01T00:00:00", "Camera", 8, 8]},
    IMPORT | {"files": [CLIP, FILE]},
    IMPORT | {"version_of": None},
    IMPORT | {"version_of": "b" * 64, "edit_of": "c" * 64},
    {"op": "favorite", "item": ITEM},
    {"op": "album.add", "item": ITEM, "kind": "user", "album": "Trip"},
    {"op": "album.rename", "kind": "user", "album": "Trip", "to": "Trip 2"},
])
def test_well_formed_events_parse(fields):
    line = json.dumps({"v": 1, "id": "x", "ts": "t", "copy": "c"} | fields).encode() + b"\n"
    assert parse_log(line, "c.jsonl")[0]["op"] == fields["op"]


@pytest.mark.parametrize("fields", [
    {k: v for k, v in IMPORT.items() if k != "files"},
    IMPORT | {"files": []},
    IMPORT | {"files": [{"role": "main", "name": "A.JPG"}]},
    IMPORT | {"files": [{k: v for k, v in FILE.items() if k != "size"}]},
    IMPORT | {"files": [FILE | {"size": "5"}]},
    IMPORT | {"files": [FILE | {"size": True}]},
    IMPORT | {"files": [FILE | {"size": -1}]},
    IMPORT | {"files": [CLIP]},
    IMPORT | {"files": [FILE, FILE | {"name": "B.JPG"}]},
    IMPORT | {"files": [FILE | {"sha256": "c" * 64}]},
    IMPORT | {"files": ["A.JPG"]},
    IMPORT | {"item": "not-a-sha"},
    IMPORT | {"folder": 2024},
    IMPORT | {"fingerprint": [["nested"]]},
    IMPORT | {"version_of": "b"},
    IMPORT | {"edit_of": 7},
    {"op": "trash"},
    {"op": "purge", "item": 7},
    {"op": "copy.name"},
    {"op": "album.add", "item": ITEM, "album": "Trip"},
    {"op": "album.remove", "kind": "user", "album": "Trip"},
    {"op": "album.rename", "kind": "user", "album": "Trip"},
    {"op": "future.thing", "item": ["unhashable"]},
])
def test_known_op_without_its_fields_is_corrupt(fields):
    line = json.dumps({"v": 1, "id": "x", "ts": "t", "copy": "c"} | fields).encode() + b"\n"
    with pytest.raises(LogCorrupt, match=rf"c\.jsonl: line 1 is not a valid {re.escape(fields['op'])} event"):
        parse_log(line, "c.jsonl")


def _short_writes(monkeypatch, most: int) -> None:
    """os.write takes at most `most` bytes of a log line per call, as a filling disk may."""
    real = os.write

    def write(fd, data):
        return real(fd, bytes(data)[:most]) if b'"op":' in bytes(data) else real(fd, data)

    monkeypatch.setattr(os, "write", write)


def test_a_short_write_is_finished(lib, monkeypatch):
    _short_writes(monkeypatch, most=7)
    append(lib, [new_event(lib, "favorite", item="a" * 64), new_event(lib, "hide", item="a" * 64)])
    monkeypatch.undo()
    assert [e["op"] for e in read_log(lib.log_path())] == ["copy.name", "favorite", "hide"]


def test_a_write_that_takes_nothing_fails(lib, monkeypatch):
    _short_writes(monkeypatch, most=0)
    with pytest.raises(OSError, match="no bytes"):
        append(lib, [new_event(lib, "favorite", item="a" * 64)])
    monkeypatch.undo()
    assert [e["op"] for e in read_log(lib.log_path())] == ["copy.name"]



@pytest.mark.parametrize("fails", ["disk full", "interrupted", "another writer", "fsync"])
def test_a_failed_append_leaves_the_log_as_it_was(lib, monkeypatch, fails):
    """Whole lines a failed append left would read as acknowledged: a re-run would take them for done."""
    before = lib.log_path().read_bytes()
    real_write, wrote = os.write, []

    def write(fd, data):
        data = bytes(data)
        if b'"op":' not in data:
            return real_write(fd, data)
        if fails == "another writer":  # a line lands beside the append, so the size check fails
            real_write(fd, b'{"v":1}\n')
            return real_write(fd, data)
        if wrote:
            if fails == "interrupted":
                raise KeyboardInterrupt
            raise OSError(errno.ENOSPC, "No space left on device")
        wrote.append(data)
        return real_write(fd, data[:data.index(b"\n") + 1])  # the first line only

    def fsync(fd):
        raise OSError(errno.EIO, "Input/output error")

    if fails == "fsync":
        monkeypatch.setattr(os, "fsync", fsync)
    else:
        monkeypatch.setattr(os, "write", write)
    with pytest.raises((OSError, LogCorrupt, KeyboardInterrupt)):
        append(lib, [new_event(lib, "favorite", item="a" * 64), new_event(lib, "hide", item="a" * 64)])
    monkeypatch.undo()
    assert lib.log_path().read_bytes() == before

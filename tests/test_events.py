import multiprocessing

import pytest

from photokeep.events import LogCorrupt, append, new_event, read_all, read_log
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

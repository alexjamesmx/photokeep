import json

from photokeep import events
from photokeep.cli import main
from photokeep.status import status
from tests.helpers import T2, add_item, ev, imp, mark


def test_status_counts_after_imports_and_a_trash(a, b):
    x = add_item(a, b"photo", name="A.JPG")
    y = add_item(a, b"second!", name="B.JPG")
    events.append(a, [imp("e" * 64, name="L.JPG", live="L.MOV", copy=a.copy_id)])
    mark(a, "trash", y)
    s = status(a)
    assert x and s["last_event_ts"] == max(e["ts"] for e in events.read_all(a))
    assert s == {"library_id": a.library_id, "copy_id": a.copy_id, "copy_name": "laptop",
                 "items_live": 2, "items_trashed": 1, "bytes_live": len(b"photo") + 2,  # L.JPG and L.MOV: 1 each
                 "copies": {a.copy_id: "laptop"}, "last_event_ts": s["last_event_ts"]}


def test_copy_names_come_from_events_and_empty_log_has_no_ts(a, b):
    assert status(a)["copies"] == {a.copy_id: "laptop"}
    events.append(a, [ev("copy.name", copy=b.copy_id, name="drive", ts=T2)])
    assert status(a)["copies"] == {a.copy_id: "laptop", b.copy_id: "drive"}


def test_cli_status(a, capsys):
    add_item(a, b"photo")
    assert main(["status", str(a.root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["items_live"] == 1
    assert main(["status", str(a.root)]) == 0
    assert "laptop" in capsys.readouterr().out


def test_cli_status_refuses_an_import_without_sizes(a, capsys):
    bad = imp("e" * 64, copy=a.copy_id)
    del bad["files"][0]["size"]
    events.append(a, [bad])
    assert main(["status", str(a.root)]) == 1
    assert "not a valid import event" in capsys.readouterr().err

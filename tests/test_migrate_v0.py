import json
import re
from datetime import UTC, datetime, timedelta

import pytest

from photokeep import events, fsops
from photokeep.cache import Cache
from photokeep.cli import main
from photokeep.events import read_all, ts_str
from photokeep.migrate_v0 import MigrationError, migrate
from photokeep.state import fold
from photokeep.verify import verify
from tests.helpers import add_item, fail_after, jpeg_bytes, make_v0, snapshot, tree

IMPORT_ID = re.compile(r"\d{4}-\d\d-\d\d-icloud-[0-9a-f]{6}")


def run(v, lib, dry_run=False):
    return migrate(v.root, v.raw, [v.zip], lib, dry_run)


def test_migrated_copy_verifies_with_original_names(tmp_path, lib):
    v = make_v0(tmp_path)
    run(v, lib)
    assert verify(lib).ok
    state = fold(read_all(lib))
    a, b, c = (state.items[v.shas[k]] for k in "abc")
    assert {f["role"]: f["name"] for f in a.files} == {"main": "IMG_0001.JPG", "live": "IMG_0001.mov"}
    assert (lib.originals / "2024/03/IMG_0001.mov").read_bytes() == (v.root / "2024/2024-03/2024-03-05 10.00.00.mov").read_bytes()
    assert (lib.originals / "undated/IMG_0003.JPG").is_file() and c.folder == "undated"
    assert (lib.trash / b.id / "IMG_0002.JPG").is_file() and b.status == "trashed"
    assert a.fingerprint == ("2024-03-05T10:00:00", "iPhone 15", 8, 8)


def test_summary_counts(tmp_path, lib):
    v = make_v0(tmp_path)
    size = sum((v.root / p).stat().st_size for p in ("2024/2024-03/2024-03-05 10.00.00.jpg",
                                                     "2024/2024-03/2024-03-05 10.00.00.mov",
                                                     ".library/trash/2.jpg", "Undated/IMG_0003.JPG"))
    assert run(v, lib) == {"items": 3, "trashed": 1, "live_clips": 1, "tombstones": 1, "favorites": 1, "hidden": 1,
                           "album_links": 2, "memory_links": 1, "copied_bytes": size, "problems": []}


def test_marks_reach_the_viewer(tmp_path, lib):
    v = make_v0(tmp_path)
    run(v, lib)
    rows = {r["id"]: r for r in Cache(lib).items_json()}
    a, b, c = (rows[v.shas[k]] for k in "abc")
    assert (a["f"], a["hi"], a["al"], a["me"], a["lv"]) == (1, 0, ["Trip"], ["Summer"], 1)
    assert (c["f"], c["hi"], c["al"], c["me"]) == (0, 1, ["Trip"], [])
    assert (b["tr"], b["r"]) == (1, "Deleted in Photos")
    assert a["o"] == "part1/Photos/IMG_0001.JPG"  # read back from sources/


def test_zip_photo_missing_from_library_is_blocked(tmp_path, lib):
    v = make_v0(tmp_path)
    run(v, lib)
    state = fold(read_all(lib))
    # the CSV and the nested zip are not media, so they are no tombstones
    assert state.tombstones == {v.shas["extra"]: "IMG_0099.JPG"}
    assert state.is_blocked(v.shas["extra"])
    assert not state.is_blocked(v.shas["a"]) and not state.is_blocked(v.shas["clip"])


def test_dry_run_writes_nothing_and_counts_the_same(tmp_path, lib):
    v = make_v0(tmp_path)
    old, new = tree(tmp_path / "v0"), snapshot(lib)
    summary = run(v, lib, dry_run=True)
    assert tree(tmp_path / "v0") == old and snapshot(lib) == new
    assert summary == run(v, lib)


def test_prototype_is_left_untouched(tmp_path, lib):
    v = make_v0(tmp_path)
    before = tree(tmp_path / "v0")
    run(v, lib)
    assert tree(tmp_path / "v0") == before


def test_sources_hold_the_export_metadata(tmp_path, lib):
    v = make_v0(tmp_path)
    run(v, lib)
    (folder,) = lib.sources.iterdir()
    assert IMPORT_ID.fullmatch(folder.name)
    for rel in ("part1/Photos/Photo Details.csv", "part2/Photos/Photo Details.csv", "part1/Albums/Trip.csv",
                "part1/Memories/Summer.csv"):
        assert (folder / rel).read_bytes() == (v.raw / rel).read_bytes()
    assert (folder / "exif.json").read_bytes() == (v.raw.parent / "exif.json").read_bytes()
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["import"] == folder.name and manifest["adapter"] == "icloud"
    clip = (v.root / "2024/2024-03/2024-03-05 10.00.00.mov").read_bytes()
    assert {"source_path": "part1/Photos/IMG_0001.mov", "sha256": v.shas["clip"], "size": len(clip),
            "mtime": None} in manifest["files"]
    assert sorted(f["source_path"] for f in manifest["files"]) == [
        "part1/Photos/IMG_0001.JPG", "part1/Photos/IMG_0001.mov", "part1/Photos/IMG_0002.JPG",
        "part2/Photos/IMG_0003.JPG"]
    assert {e["import"] for e in read_all(lib) if e["op"] == "import"} == {folder.name}


def test_thumbnails_and_measurements_come_along(tmp_path, lib):
    v = make_v0(tmp_path)
    run(v, lib)
    cache = Cache(lib)
    a, b, c = (v.shas[k] for k in "abc")
    assert cache.thumb_path(a).read_bytes() == b"thumb of A" and cache.preview_path(a).read_bytes() == b"preview of A"
    rows = {r["id"]: r for r in cache.items_json()}
    assert (rows[a]["th"], rows[a]["pv"], rows[a]["sh"], rows[a]["br"], rows[a]["no"]) == (1, 1, 12.5, 100.0, 1.5)
    assert (rows[b]["th"], rows[b]["pv"]) == (1, 0)
    assert rows[c]["th"] == 0  # the prototype had none, so the next refresh renders it


def test_trashed_yesterday_folds_as_trashed_at_that_time(tmp_path, lib, mexico):
    v = make_v0(tmp_path)
    run(v, lib)
    b = fold(read_all(lib)).items[v.shas["b"]]
    expected = (datetime.fromisoformat(v.trashed_at) + timedelta(hours=6)).replace(tzinfo=UTC)  # UTC-6
    assert (b.status, b.trashed_at) == ("trashed", ts_str(expected))
    assert {r["id"]: r for r in Cache(lib).items_json()}[b.id]["left"] == 29


def test_interrupted_copy_is_finished_by_running_again(tmp_path, lib, monkeypatch):
    v = make_v0(tmp_path)
    monkeypatch.setattr(fsops, "copy_verified", fail_after(2))
    with pytest.raises(fsops.CopyError):
        run(v, lib)
    assert fold(read_all(lib)).items == {}
    monkeypatch.undo()
    run(v, lib)
    assert verify(lib).ok
    assert sorted(e["item"] for e in read_all(lib) if e["op"] == "import") == sorted(v.shas[k] for k in "abc")


def test_run_cut_off_before_the_log_reuses_its_sources(tmp_path, lib, monkeypatch):
    v = make_v0(tmp_path)

    def cut_off(*_):
        raise OSError("power lost")

    monkeypatch.setattr(events, "append", cut_off)
    with pytest.raises(OSError):
        run(v, lib)
    monkeypatch.undo()
    run(v, lib)
    (folder,) = lib.sources.iterdir()
    assert {e["import"] for e in read_all(lib) if e["op"] == "import"} == {folder.name}
    assert verify(lib).ok


def test_refuses_a_copy_that_has_items(tmp_path, lib):
    v = make_v0(tmp_path)
    add_item(lib, b"photo")
    with pytest.raises(MigrationError, match="already has items"):
        run(v, lib)


def test_duplicates_merge_and_bad_rows_are_skipped(tmp_path, lib):
    v = make_v0(tmp_path)
    c = (v.root / "Undated/IMG_0003.JPG").read_bytes()
    dup = v.add(c, "Undated/IMG_0004.JPG", "part2/IMG_0004.JPG", favorite=1, albums=["Other"], memories=["Winter"])
    bad = v.add(jpeg_bytes((1, 2, 3)), "Undated/IMG_0005.JPG", "part2/IMG_0005.JPG", sha256="0" * 64)
    gone = v.add(None, "Undated/IMG_0006.JPG", "part2/IMG_0006.JPG", sha256="1" * 64)
    summary = run(v, lib)
    assert summary["items"] == 3
    assert [p["source"].split()[1] for p in summary["problems"]] == [str(bad), str(gone), str(dup)]
    item = fold(read_all(lib)).items[v.shas["c"]]
    assert (item.favorite, item.hidden) == (True, True)
    assert item.albums == {("album", "Trip"), ("album", "Other"), ("memory", "Winter")}
    assert verify(lib).ok


def test_clip_without_identifier_is_named_after_its_photo(tmp_path, lib):
    v = make_v0(tmp_path)
    v.add(jpeg_bytes((5, 5, 5)), "2024/2024-04/a.jpg", "part2/IMG_0007.HEIC", clip=b"clip 7",
          live="2024/2024-04/a.mov", taken="2024-04-01T08:00:00")
    run(v, lib)
    assert (lib.originals / "2024/04/IMG_0007.MOV").read_bytes() == b"clip 7"


def test_cli_dry_run_prints_json_and_progress(tmp_path, lib, capsys):
    v = make_v0(tmp_path)
    code = main(["migrate-v0", str(v.root), str(lib.root), "--raw", str(v.raw), "--zip", str(v.zip),
                 "--dry-run", "--json"])
    out, err = capsys.readouterr()
    assert code == 0 and json.loads(out)["items"] == 3
    assert "hashing" in err
    assert fold(read_all(lib)).items == {}


def test_cli_text_lists_problems_and_exits_2(tmp_path, lib, capsys):
    v = make_v0(tmp_path)
    gone = v.add(None, "Undated/IMG_0006.JPG", "part2/IMG_0006.JPG", sha256="1" * 64)
    code = main(["migrate-v0", str(v.root), str(lib.root), "--raw", str(v.raw), "--zip", str(v.zip), "--dry-run"])
    out = capsys.readouterr().out
    assert code == 2
    assert "items: 3" in out and f"problems: 1 (row {gone} (Undated/IMG_0006.JPG): file missing" in out
    assert "dry run: nothing changed" in out

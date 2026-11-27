import json
import re
import zipfile
from datetime import UTC, datetime, timedelta

import pytest

from photokeep import events, fsops, zipscan
from photokeep.cache import Cache
from photokeep.cli import main
from photokeep.events import read_all, ts_str
from photokeep.library import init_library
from photokeep.migrate_v0 import MigrationError, migrate
from photokeep.state import fold
from photokeep.verify import verify
from tests.helpers import add_item, exit_worker, fail_after, jpeg_bytes, make_v0, sha, snapshot, tree

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
    with pytest.raises(MigrationError, match="already has items") as refused:
        run(v, lib)
    assert f"photokeep cache refresh {lib.root}" in str(refused.value)
    assert f"photokeep verify {lib.root}" in str(refused.value)


@pytest.mark.parametrize("where", ["copy in prototype", "copy in raw", "prototype in copy"])
def test_refuses_a_copy_overlapping_the_prototype(tmp_path, where):
    if where == "prototype in copy":
        lib = init_library(tmp_path / "D", "laptop")
        v = make_v0(lib.root)
    else:
        v = make_v0(tmp_path)
        lib = init_library((v.root if where == "copy in prototype" else v.raw) / "copy", "laptop")
    with pytest.raises(MigrationError, match="overlap"):
        run(v, lib)
    assert fold(read_all(lib)).items == {}


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


def test_duplicate_with_a_clip_is_the_one_kept(tmp_path, lib):
    v = make_v0(tmp_path)
    c = (v.root / "Undated/IMG_0003.JPG").read_bytes()  # C has no clip
    kept = v.add(c, "Undated/IMG_0004.JPG", "part2/IMG_0004.JPG", clip=b"clip 4", live="Undated/IMG_0004.mov")
    other = v.add(c, "Undated/IMG_0005.JPG", "part2/IMG_0005.JPG", clip=b"clip 5", live="Undated/IMG_0005.mov")
    summary = run(v, lib)
    item = fold(read_all(lib)).items[v.shas["c"]]
    assert {f["role"]: f["name"] for f in item.files} == {"main": "IMG_0004.JPG", "live": "IMG_0004.MOV"}
    assert (lib.originals / "undated/IMG_0004.MOV").read_bytes() == b"clip 4"
    assert {"source": f"row {other} (Undated/IMG_0005.JPG)",
            "problem": f"live clip Undated/IMG_0005.mov not migrated (duplicate of row {kept})"} in summary["problems"]
    assert (summary["items"], summary["live_clips"]) == (3, 2) and verify(lib).ok


def test_first_duplicate_in_the_trash_is_not_the_one_kept(tmp_path, lib):
    v = make_v0(tmp_path)
    data = jpeg_bytes((7, 7, 7))
    v.add(data, ".library/trash/10.jpg", "part1/IMG_0010.JPG", taken="2023-01-01T00:00:00", trashed=1,
          trashed_at=v.trashed_at, reason="Exact duplicate of part1/IMG_0011.JPG")
    v.add(data, "2023/2023-02/2023-02-01 00.00.00.jpg", "part1/IMG_0011.JPG", taken="2023-02-01T00:00:00")
    run(v, lib)
    item = fold(read_all(lib)).items[sha(data)]
    assert (item.status, item.folder, item.files[0]["name"]) == ("live", "2023/02", "IMG_0011.JPG")
    assert (lib.originals / "2023/02/IMG_0011.JPG").read_bytes() == data


def test_trashed_item_keeps_its_marks(tmp_path, lib):
    v = make_v0(tmp_path)
    data = jpeg_bytes((8, 8, 8))
    v.add(data, ".library/trash/20.jpg", "part1/IMG_0020.JPG", trashed=1, trashed_at=v.trashed_at,
          favorite=1, albums=["Trip"], memories=["Summer"])
    run(v, lib)
    item = fold(read_all(lib)).items[sha(data)]
    assert (item.status, item.favorite) == ("trashed", True)
    assert item.albums == {("album", "Trip"), ("memory", "Summer")}
    assert (lib.trash / item.id / "IMG_0020.JPG").read_bytes() == data


def test_clip_of_the_wrong_size_skips_its_row(tmp_path, lib):
    v = make_v0(tmp_path)
    row = v.add(jpeg_bytes((6, 6, 6)), "2024/2024-04/b.jpg", "part2/IMG_0008.JPG", clip=b"clip 8",
                live="2024/2024-04/b.mov", live_size=999)
    summary = run(v, lib)
    assert summary["problems"] == [{"source": f"row {row} (2024/2024-04/b.jpg)",
                                    "problem": "live clip is 6 bytes, the database says 999"}]
    assert summary["items"] == 3


def test_damaged_zip_member_is_a_problem_not_a_tombstone(tmp_path, lib):
    v = make_v0(tmp_path)
    bad, body = tmp_path / "bad.zip", b"photo bytes " * 50
    with zipfile.ZipFile(bad, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("Part/Photos/IMG_0100.JPG", body)
    bad.write_bytes(bad.read_bytes().replace(body, b"X" + body[1:]))  # same length: only the CRC check fails
    summary = migrate(v.root, v.raw, [v.zip, bad], lib, False)
    assert summary["problems"] == [{"source": "bad.zip:Part/Photos/IMG_0100.JPG", "problem": "unreadable zip member"}]
    assert set(fold(read_all(lib)).tombstones) == {v.shas["extra"]}


def _bad_exif(v, monkeypatch):
    (v.raw.parent / "exif.json").write_text("{not json")


def _bad_albums(v, monkeypatch):
    v.add(jpeg_bytes((4, 4, 4)), "Undated/IMG_0009.JPG", "part2/IMG_0009.JPG", albums="Trip")


def _dying_worker(v, monkeypatch):
    monkeypatch.setattr(zipscan, "_sha", exit_worker)


@pytest.mark.parametrize("spoil", [_bad_exif, _bad_albums, _dying_worker])
def test_bad_input_is_a_one_line_error_before_any_write(tmp_path, lib, capsys, monkeypatch, spoil):
    v = make_v0(tmp_path)
    spoil(v, monkeypatch)
    before = snapshot(lib)
    code = main(["migrate-v0", str(v.root), str(lib.root), "--raw", str(v.raw), "--zip", str(v.zip)])
    out, err = capsys.readouterr()
    errors = [line for line in err.splitlines() if not line.startswith("hashing")]
    assert (code, out) == (1, "") and len(errors) == 1 and errors[0].startswith("photokeep: ")
    assert snapshot(lib) == before


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


def test_cli_with_relative_paths(tmp_path, lib, monkeypatch):
    v = make_v0(tmp_path)
    monkeypatch.chdir(tmp_path)
    rel = [str(p.relative_to(tmp_path)) for p in (v.root, lib.root, v.raw, v.zip)]
    assert main(["migrate-v0", rel[0], rel[1], "--raw", rel[2], "--zip", rel[3]]) == 0
    assert main(["verify", rel[1]]) == 0

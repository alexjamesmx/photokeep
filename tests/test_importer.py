import dataclasses
import errno
import io
import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from photokeep import events, importer, staging, sync
from photokeep.cache import Cache
from photokeep.events import read_all
from photokeep.importer import Candidate, ImportFailed, SourceFile
from photokeep.library import init_library
from photokeep.metadata import provider_record
from photokeep.state import fold
from photokeep.verify import verify
from tests.helpers import T1, add_item, ev, exif_jpeg, imp, jpeg_bytes, sha, snapshot

needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool is not installed")
IMPORT_ID = re.compile(r"\d{4}-\d\d-\d\d-test-[0-9a-f]{6}")
FP = ["2024-03-05T10:00:00", "Pixel 7", 8, 6]  # exif_jpeg's fingerprint


def cand(path: str, data: bytes, *, mtime: str | None = None, provider: dict | None = None, **marks) -> Candidate:
    """A candidate of an in-memory source, keeping its file name."""
    return Candidate(path, lambda: io.BytesIO(data), len(data), mtime, PurePosixPath(path).name, provider or {},
                     **marks)


def unplugged() -> io.BytesIO:
    raise OSError("source unplugged")


def run(lib, *cands: Candidate, sources: tuple[SourceFile, ...] = (), dry_run: bool = False) -> dict:
    return importer.run(lib, "test", list(cands), list(sources), dry_run, datetime.now(UTC))


def staged(lib) -> list[str]:
    top = lib.cache / "import"
    return sorted(p.name for p in top.iterdir()) if top.exists() else []


def manifest(lib) -> dict:
    (folder,) = lib.sources.iterdir()
    return json.loads((folder / "manifest.json").read_text(encoding="utf-8"))


def summary(**counts) -> dict:
    zero = dict.fromkeys(("imported", "versions", "edits", "live_clips", "already_in_library", "duplicates_in_source",
                          "skipped_deleted", "trashed", "album_links", "favorites", "bytes"), 0)
    return zero | {"problems": []} | counts


@needs_exiftool
def test_new_photo_lands_in_its_month_with_its_event_and_fingerprint(lib):
    data = exif_jpeg()
    got = run(lib, cand("Photos from 2024/IMG_1.JPG", data, mtime="2024-03-06T00:00:00Z"))
    assert got == summary(imported=1, bytes=len(data))
    assert (lib.originals / "2024/03/IMG_1.JPG").read_bytes() == data
    (e,) = [e for e in read_all(lib) if e["op"] == "import"]
    assert IMPORT_ID.fullmatch(e["import"])
    assert (e["item"], e["folder"], e["fingerprint"]) == (sha(data), "2024/03", FP)
    assert e["files"] == [{"role": "main", "name": "IMG_1.JPG", "sha256": sha(data), "size": len(data),
                           "mtime": "2024-03-06T00:00:00Z"}]
    assert "version_of" not in e and "edit_of" not in e
    assert verify(lib).ok
    assert [r["id"] for r in Cache(lib).items_json()] == [sha(data)]
    assert staged(lib) == []


@needs_exiftool
def test_sources_keep_the_metadata_files_manifest_and_exif(lib):
    data = exif_jpeg()
    sidecar = SourceFile("Photos from 2024/IMG_1.JPG.json", lambda: b'{"title": "IMG_1.JPG"}')
    run(lib, cand("Photos from 2024/IMG_1.JPG", data), sources=(sidecar,))
    (folder,) = lib.sources.iterdir()
    assert (folder / "Photos from 2024/IMG_1.JPG.json").read_bytes() == b'{"title": "IMG_1.JPG"}'
    m = manifest(lib)
    assert (m["import"], m["adapter"], m["exif_root"]) == (folder.name, "test", folder.name)
    assert m["files"] == [{"source_path": "Photos from 2024/IMG_1.JPG", "sha256": sha(data), "size": len(data),
                           "mtime": None, "path": "originals/2024/03/IMG_1.JPG"}]
    (record,) = json.loads((folder / "exif.json").read_bytes())
    assert (record["SourceFile"], record["Model"]) == (f"{folder.name}/Photos from 2024/IMG_1.JPG", "Pixel 7")
    assert isinstance(record["ImageWidth"], int)  # -n: numbers, as in the migrated library's exif.json


@needs_exiftool
def test_provider_date_files_a_photo_without_one(lib):
    data = jpeg_bytes()
    run(lib, cand("a/IMG_1.JPG", data, provider=provider_record(taken="2023-07-15T12:00:00Z")))
    assert (lib.originals / "2023/07/IMG_1.JPG").read_bytes() == data


@needs_exiftool
def test_bytes_already_in_the_library_add_no_event(lib):
    old, new = exif_jpeg((1, 2, 3)), exif_jpeg((9, 9, 9))
    add_item(lib, old)
    before = snapshot(lib)
    assert run(lib, cand("a/OLD.JPG", old)) == summary(already_in_library=1)
    assert snapshot(lib) == before  # nothing new: not even a sources/ folder
    got = run(lib, cand("a/OLD.JPG", old), cand("a/NEW.JPG", new))
    assert got == summary(imported=1, already_in_library=1, bytes=len(new))
    assert [e["item"] for e in read_all(lib) if e["op"] == "import"] == [sha(old), sha(new)]
    assert [(f["source_path"], f["path"]) for f in manifest(lib)["files"]] == [
        ("a/OLD.JPG", None), ("a/NEW.JPG", "originals/2024/03/NEW.JPG")]


@needs_exiftool
@pytest.mark.parametrize("how", ["trash", "purge"])
@pytest.mark.parametrize("arrives_as", ["the same bytes", "a re-encode"])
def test_deleted_photo_is_not_imported_again(lib, how, arrives_as):
    gone = exif_jpeg((1, 2, 3))
    events.append(lib, [imp(sha(gone), fingerprint=FP, copy=lib.copy_id), ev(how, sha(gone), ts=T1, copy=lib.copy_id)])
    before = snapshot(lib)
    again = gone if arrives_as == "the same bytes" else exif_jpeg((9, 9, 9))
    assert run(lib, cand("a/IMG_1.JPG", again)) == summary(skipped_deleted=1)
    assert snapshot(lib) == before


@needs_exiftool
def test_fingerprint_of_a_live_item_makes_a_version(lib):
    first, other = exif_jpeg((1, 2, 3)), exif_jpeg((9, 9, 9))
    events.append(lib, [imp(sha(first), fingerprint=FP, copy=lib.copy_id)])
    assert run(lib, cand("a/IMG_1.JPG", other)) == summary(imported=1, versions=1, bytes=len(other))
    assert fold(read_all(lib)).items[sha(other)].version_of == sha(first)


@needs_exiftool
def test_live_clip_and_edit_are_linked_to_their_photo(lib):
    photo, edit, clip = exif_jpeg((1, 2, 3)), exif_jpeg((9, 9, 9)), b"live clip"
    got = run(lib, cand("a/IMG_1-edited.JPG", edit, edit_of="a/IMG_1.JPG"),
              cand("a/IMG_1.MP4", clip, live_of="a/IMG_1.JPG"), cand("a/IMG_1.JPG", photo))
    assert got == summary(imported=2, edits=1, live_clips=1, bytes=len(photo) + len(edit) + len(clip))
    state = fold(read_all(lib))
    assert {f["role"]: f["name"] for f in state.items[sha(photo)].files} == {"main": "IMG_1.JPG", "live": "IMG_1.MP4"}
    assert (lib.originals / "2024/03/IMG_1.MP4").read_bytes() == clip
    assert sha(clip) not in state.items
    assert state.items[sha(edit)].edit_of == sha(photo)
    assert verify(lib).ok


@needs_exiftool
def test_clip_is_skipped_with_its_photo(lib):
    photo = exif_jpeg()
    add_item(lib, photo)
    got = run(lib, cand("a/IMG_1.JPG", photo), cand("a/IMG_1.MP4", b"live clip", live_of="a/IMG_1.JPG"))
    assert got == summary(already_in_library=2)


@needs_exiftool
def test_edit_of_an_original_not_imported_keeps_no_link(lib):
    gone, edit = exif_jpeg((1, 2, 3)), exif_jpeg((9, 9, 9))
    events.append(lib, [imp(sha(gone), copy=lib.copy_id), ev("trash", sha(gone), ts=T1, copy=lib.copy_id)])
    got = run(lib, cand("a/IMG_1.JPG", gone), cand("a/IMG_1-edited.JPG", edit, edit_of="a/IMG_1.JPG"))
    assert got["problems"] == [{"source_path": "a/IMG_1-edited.JPG",
                                "reason": "its original a/IMG_1.JPG was not imported: kept without the link"}]
    assert (got["imported"], got["edits"], got["skipped_deleted"]) == (1, 0, 1)
    assert fold(read_all(lib)).items[sha(edit)].edit_of is None


@needs_exiftool
def test_same_bytes_twice_are_one_item_in_every_album(lib):
    data = exif_jpeg()
    got = run(lib, cand("Photos from 2024/IMG_1.JPG", data), cand("Trip/IMG_1.JPG", data, albums=("Trip",)),
              cand("Beach/IMG_1.JPG", data, albums=("Beach", "Trip")))
    assert got == summary(imported=1, duplicates_in_source=2, album_links=2, bytes=len(data))
    assert fold(read_all(lib)).items[sha(data)].albums == {("album", "Trip"), ("album", "Beach")}
    assert [f["source_path"] for f in manifest(lib)["files"]] == [
        "Photos from 2024/IMG_1.JPG", "Trip/IMG_1.JPG", "Beach/IMG_1.JPG"]


@needs_exiftool
def test_two_photos_of_one_name_in_a_month_never_overwrite(lib):
    a, b = exif_jpeg((1, 2, 3)), exif_jpeg((9, 9, 9))
    run(lib, cand("x/IMG_1.JPG", a), cand("y/IMG_1.JPG", b))
    assert (lib.originals / "2024/03/IMG_1.JPG").read_bytes() == a
    assert (lib.originals / f"2024/03/IMG_1~{sha(b)[:8]}.JPG").read_bytes() == b
    assert verify(lib).ok


@needs_exiftool
def test_trashed_in_the_source_is_imported_then_trashed(lib):
    data = exif_jpeg()
    got = run(lib, cand("a/IMG_1.JPG", data, trashed=True, favorite=True))
    assert got == summary(imported=1, trashed=1, favorites=1, bytes=len(data))
    mine = [e for e in read_all(lib) if e.get("item") == sha(data)]
    assert [e["op"] for e in mine] == ["import", "favorite", "trash"]
    assert mine[0]["ts"] < mine[1]["ts"] < mine[2]["ts"]
    assert mine[2]["reason"] == "Trashed in test"
    item = fold(read_all(lib)).items[sha(data)]
    assert (item.status, item.favorite) == ("trashed", True)
    assert (lib.trash / sha(data) / "IMG_1.JPG").read_bytes() == data
    assert verify(lib).ok


@needs_exiftool
def test_trash_in_an_import_does_not_block_a_version_in_it(lib):
    a, b = exif_jpeg((1, 2, 3)), exif_jpeg((9, 9, 9))  # one fingerprint
    run(lib, cand("x/IMG_1.JPG", a, trashed=True), cand("y/IMG_2.JPG", b))
    assert fold(read_all(lib)).items[sha(b)].status == "live"
    assert verify(lib).ok


@needs_exiftool
def test_unreadable_files_are_problems_and_the_rest_imports(lib):
    good = exif_jpeg()
    short = dataclasses.replace(cand("a/SHORT.JPG", b"xx"), size=5)
    got = run(lib, dataclasses.replace(cand("a/GONE.JPG", b"xx"), read=unplugged), short, cand("a/IMG_1.JPG", good))
    assert got == summary(imported=1, bytes=len(good), problems=[
        {"source_path": "a/GONE.JPG", "reason": "could not be read: source unplugged"},
        {"source_path": "a/SHORT.JPG", "reason": "2 bytes read, 5 listed"}])
    assert [f["sha256"] for f in manifest(lib)["files"]][:2] == [None, None]
    assert verify(lib).ok


@needs_exiftool
def test_dry_run_changes_no_truth_layer_and_counts_the_same(lib):
    cands = [cand("a/IMG_1.JPG", exif_jpeg(), trashed=True), cand("a/IMG_1.MP4", b"clip", live_of="a/IMG_1.JPG"),
             cand("b/IMG_1.JPG", exif_jpeg((1, 2, 3)), albums=("Trip",))]
    before = snapshot(lib)
    got = run(lib, *cands, dry_run=True)
    assert snapshot(lib) == before and staged(lib) == []
    assert got == run(lib, *cands)
    assert got["imported"] == 2


@needs_exiftool
def test_run_cut_off_before_the_log_is_finished_without_reading_the_source_again(lib, monkeypatch):
    a, b = exif_jpeg((1, 2, 3)), exif_jpeg((9, 9, 9))
    cands = [cand("x/IMG_1.JPG", a), cand("x/IMG_2.JPG", b, albums=("Trip",))]
    real = events.append

    def cut_off(*_):
        monkeypatch.setattr(events, "append", real)
        raise OSError("power lost")

    monkeypatch.setattr(events, "append", cut_off)
    with pytest.raises(OSError, match="power lost"):
        run(lib, *cands)
    assert staged(lib)  # kept for the next run
    got = run(lib, *(dataclasses.replace(c, read=unplugged) for c in cands))
    assert got == summary(imported=2, album_links=1, bytes=len(a) + len(b))
    (folder,) = lib.sources.iterdir()
    imports = [e for e in read_all(lib) if e["op"] == "import"]
    assert sorted(e["item"] for e in imports) == sorted([sha(a), sha(b)])
    assert {e["import"] for e in imports} == {folder.name}
    assert [e["op"] for e in read_all(lib)].count("album.add") == 1
    assert verify(lib).ok and staged(lib) == []


@needs_exiftool
def test_files_staged_whole_are_not_read_again_and_damaged_ones_are(lib, monkeypatch):
    a, b = exif_jpeg((1, 2, 3)), exif_jpeg((9, 9, 9))
    cands = [cand("x/IMG_1.JPG", a), cand("x/IMG_2.JPG", b)]
    def cut_off(*_):
        raise OSError("power lost")

    monkeypatch.setattr(staging.Stage, "exif", cut_off)
    with pytest.raises(OSError, match="power lost"):
        run(lib, *cands)
    monkeypatch.undo()
    (folder,) = (p for p in (lib.cache / "import").iterdir() if p.is_dir())
    (folder / "x/IMG_2.JPG").write_bytes(b"rotted")
    got = run(lib, dataclasses.replace(cands[0], read=unplugged), cands[1])
    assert got == summary(imported=2, bytes=len(a) + len(b))
    assert verify(lib).ok


@needs_exiftool
def test_run_cut_off_while_moving_files_is_finished_by_running_again(lib, monkeypatch):
    a, b = exif_jpeg((1, 2, 3)), exif_jpeg((9, 9, 9))
    cands = [cand("x/IMG_1.JPG", a), cand("x/IMG_2.JPG", b)]
    real, moved = staging.Stage.place, []

    def place(self, *args):
        if moved:
            raise OSError("drive unplugged")
        real(self, *args)
        moved.append(args)

    monkeypatch.setattr(staging.Stage, "place", place)
    with pytest.raises(OSError, match="drive unplugged"):
        run(lib, *cands)
    monkeypatch.undo()
    assert run(lib, *cands)["imported"] == 2
    assert verify(lib).ok and len(list(lib.sources.iterdir())) == 1


@needs_exiftool
def test_staged_file_on_another_filesystem_is_copied_verified(lib, monkeypatch):
    real = os.replace

    def replace(src, dst):
        if Path(src).is_relative_to(lib.cache) and Path(dst).is_relative_to(lib.originals):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    data = exif_jpeg()
    run(lib, cand("a/IMG_1.JPG", data))
    assert (lib.originals / "2024/03/IMG_1.JPG").read_bytes() == data
    assert verify(lib).ok


@needs_exiftool
def test_imported_copy_syncs_whole_to_another(a, b):
    data = exif_jpeg()
    importer.run(a, "test", [cand("x/IMG_1.JPG", data, albums=("Trip",))],
                 [SourceFile("x/IMG_1.JPG.json", lambda: b"{}")], False, datetime.now(UTC))
    done = sync.run(a, b, datetime.now(UTC))
    assert (done.sources_conflicts, done.failed) == ([], [])
    assert (b.originals / "2024/03/IMG_1.JPG").read_bytes() == data
    (folder,) = b.sources.iterdir()
    assert sorted(p.name for p in folder.rglob("*")) == ["IMG_1.JPG.json", "exif.json", "manifest.json", "x"]
    assert verify(b).ok and fold(read_all(b)).items[sha(data)].albums == {("album", "Trip")}


@needs_exiftool
def test_path_taken_by_an_untracked_file_stops_the_import_before_any_write(lib):
    data = exif_jpeg()
    (lib.originals / "2024/03").mkdir(parents=True)
    (lib.originals / "2024/03/IMG_1.JPG").write_bytes(b"dropped in by hand")
    before = snapshot(lib)
    with pytest.raises(ImportFailed, match="originals/2024/03/IMG_1.JPG"):
        run(lib, cand("a/IMG_1.JPG", data))
    assert snapshot(lib) == before


def test_missing_exiftool_is_a_one_line_error_before_any_write(lib, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda *_: None)
    before, cache = snapshot(lib), sorted(lib.cache.rglob("*"))
    with pytest.raises(ImportFailed, match=r"^exiftool is required: install it \(e\.g\. perl-image-exiftool / brew "
                                           r"install exiftool\)$"):
        run(lib, cand("a/IMG_1.JPG", jpeg_bytes()))
    assert snapshot(lib) == before and sorted(lib.cache.rglob("*")) == cache


@pytest.mark.parametrize("path", ["../x.jpg", "/x.jpg", "a/./x.jpg", "a//x.jpg", "a/", "C:x.jpg", "a\\..\\x.jpg"])
def test_source_path_leaving_its_folder_is_refused(lib, path):
    with pytest.raises(ImportFailed, match="unusable source path"):
        run(lib, cand(path, b"x"))
    with pytest.raises(ImportFailed, match="unusable source path"):
        run(lib, sources=(SourceFile(path, lambda: b"{}"),))


@pytest.mark.parametrize("name", ["exif.json", "manifest.json"])
def test_metadata_file_named_like_photokeeps_own_is_refused(lib, name):
    with pytest.raises(ImportFailed, match=f"unusable source path '{name}'"):
        run(lib, sources=(SourceFile(name, lambda: b"{}"),))


def test_source_path_listed_twice_is_refused(lib):
    with pytest.raises(ImportFailed, match="a/x.jpg is listed twice"):
        run(lib, cand("a/x.jpg", b"x"), cand("a/x.jpg", b"y"))
    with pytest.raises(ImportFailed, match="a/x.json is listed twice"):
        run(lib, sources=(SourceFile("a/x.json", lambda: b"{}"), SourceFile("a/x.json", lambda: b"[]")))


@pytest.mark.parametrize("where", ["source in copy", "copy in source"])
def test_refuses_a_source_overlapping_the_copy(tmp_path, where):
    if where == "source in copy":
        lib = init_library(tmp_path / "L", "laptop")
        source = lib.root / "inbox"
    else:
        source = tmp_path / "export"
        lib = init_library(source / "L", "laptop")
    with pytest.raises(ImportFailed, match="overlap"):
        importer.refuse_overlap(lib, [tmp_path / "elsewhere", source])
    importer.refuse_overlap(lib, [tmp_path / "elsewhere"])

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

from photokeep import events, fsops, importer, importplan, staging, sync
from photokeep.cache import Cache
from photokeep.events import read_all
from photokeep.importer import Candidate, ImportFailed, SourceFile
from photokeep.library import init_library
from photokeep.metadata import derive, fingerprint, provider_record
from photokeep.state import fold
from photokeep.verify import verify
from tests.helpers import T1, add_item, ev, exif_jpeg, files_of, imp, jpeg_bytes, put, sha, snapshot

needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool is not installed")
IMPORT_ID = re.compile(r"\d{4}-\d\d-\d\d-test-[0-9a-f]{6}")
FP = ["2024-03-05T10:00:00", "Pixel 7", 8, 6]  # exif_jpeg's fingerprint


def cand(path: str, data: bytes, *, mtime: str | None = None, provider: dict | None = None, **marks) -> Candidate:
    """A candidate of an in-memory source, keeping its file name."""
    return Candidate(path, lambda: io.BytesIO(data), len(data), mtime, PurePosixPath(path).name, provider or {},
                     **marks)


def unplugged() -> io.BytesIO:
    raise OSError("source unplugged")


def run(lib, *cands: Candidate, sources: tuple[SourceFile, ...] = (), dry_run: bool = False,
        roots: tuple[Path, ...] = ()) -> dict:
    return importer.run(lib, "test", list(cands), list(sources), dry_run, datetime.now(UTC), roots=roots)


def held(lib, photo: bytes, clip: bytes | None = None) -> None:
    """photo as an earlier import left it in lib, with its Live clip."""
    put(lib, "originals/2024/01/IMG_1.JPG", photo)
    if clip:
        put(lib, "originals/2024/01/IMG_1.MOV", clip)
    events.append(lib, [imp(sha(photo), copy=lib.copy_id, size=len(photo), live="IMG_1.MOV" if clip else None,
                            live_sha=sha(clip) if clip else None)])


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
def test_bytes_in_the_library_get_this_source_as_another_reference(lib):
    old = exif_jpeg((1, 2, 3))
    add_item(lib, old, name="OLD.JPG")
    before = files_of(lib)
    again = cand("Trip/OLD.JPG", old, albums=("Trip",), favorite=True, trashed=True)
    assert run(lib, again) == summary(already_in_library=1, album_links=1, favorites=1)
    assert files_of(lib) == before  # nothing moves, and a new source never trashes what the library holds
    (folder,) = lib.sources.iterdir()
    item = fold(read_all(lib)).items[sha(old)]
    assert (item.status, item.favorite, item.albums, item.imports[1:]) == ("live", True, {("album", "Trip")},
                                                                           [folder.name])
    assert [e["op"] for e in read_all(lib) if e.get("item") == sha(old)] == ["import", "import", "album.add",
                                                                             "favorite"]
    assert [(f["source_path"], f["path"]) for f in manifest(lib)["files"]] == [
        ("Trip/OLD.JPG", "originals/2024/01/OLD.JPG")]
    assert verify(lib).ok
    log = read_all(lib)
    assert run(lib, again) == summary(already_in_library=1)  # this source is recorded already
    assert read_all(lib) == log


@needs_exiftool
def test_metadata_is_kept_when_nothing_is_new(lib):
    photo, clip = exif_jpeg(), b"live clip"
    held(lib, photo, clip)
    log = read_all(lib)
    got = run(lib, cand("a/IMG_1.MOV", clip), sources=(SourceFile("a/IMG_1.MOV.json", lambda: b"{}"),))
    assert got == summary(already_in_library=1)
    assert read_all(lib) == log  # a clip's bytes are no item of their own to refer to
    (folder,) = lib.sources.iterdir()
    assert (folder / "a/IMG_1.MOV.json").read_bytes() == b"{}"
    assert manifest(lib)["files"][0]["path"] == "originals/2024/01/IMG_1.MOV"


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
def test_deleted_photo_is_skipped_in_every_copy(lib):
    # Only the copy with the provider's date has the deleted photo's fingerprint; the other has no date at all.
    dated = provider_record(taken="2023-07-15T12:00:00Z")
    fp = fingerprint(derive({"Model": "Pixel 7", "ImageWidth": 8, "ImageHeight": 6}, dated, "IMG_9.JPG", None))
    gone, again = exif_jpeg((7, 7, 7), taken=None), exif_jpeg((5, 5, 5), taken=None)
    events.append(lib, [imp(sha(gone), fingerprint=list(fp), copy=lib.copy_id),
                        ev("trash", sha(gone), ts=T1, copy=lib.copy_id)])
    got = run(lib, cand("Trip/IMG_9.JPG", again), cand("Photos from 2023/IMG_9.JPG", again, provider=dated))
    assert got == summary(skipped_deleted=2)
    assert sha(again) not in fold(read_all(lib)).items


@needs_exiftool
def test_fingerprint_of_a_live_item_makes_a_version(lib):
    first, other = exif_jpeg((1, 2, 3)), exif_jpeg((9, 9, 9))
    events.append(lib, [imp(sha(first), fingerprint=FP, copy=lib.copy_id)])
    assert run(lib, cand("a/IMG_1.JPG", other)) == summary(imported=1, versions=1, bytes=len(other))
    assert fold(read_all(lib)).items[sha(other)].version_of == sha(first)


@needs_exiftool
def test_two_versions_in_one_import_are_linked(lib):
    first, other = exif_jpeg((1, 2, 3)), exif_jpeg((9, 9, 9))  # one fingerprint
    assert run(lib, cand("x/IMG_1.JPG", first), cand("y/IMG_2.JPG", other))["versions"] == 1
    assert fold(read_all(lib)).items[sha(other)].version_of == sha(first)


@needs_exiftool
def test_live_clip_and_edit_are_linked_to_their_photo(lib):
    photo, edit, clip = exif_jpeg((1, 2, 3)), exif_jpeg((9, 9, 9)), b"live clip"
    got = run(lib, cand("a/IMG_1-edited.JPG", edit, edit_of="a/IMG_1.JPG"),
              cand("a/IMG_1.MP4", clip, live_of="a/IMG_1.JPG"), cand("a/IMG_1.JPG", photo))
    # one fingerprint, so the photo is also a version of the edit listed before it
    assert got == summary(imported=2, versions=1, edits=1, live_clips=1, bytes=len(photo) + len(edit) + len(clip))
    state = fold(read_all(lib))
    assert {f["role"]: f["name"] for f in state.items[sha(photo)].files} == {"main": "IMG_1.JPG", "live": "IMG_1.MP4"}
    assert (lib.originals / "2024/03/IMG_1.MP4").read_bytes() == clip
    assert sha(clip) not in state.items
    assert state.items[sha(edit)].edit_of == sha(photo)
    assert verify(lib).ok


@needs_exiftool
@pytest.mark.parametrize("library_clip", ["the same", "another", "none"])
def test_clip_of_a_photo_in_the_library_counts_by_its_own_bytes(lib, library_clip):
    photo, clip = exif_jpeg(), b"live clip"
    held(lib, photo, {"the same": clip, "another": b"another clip", "none": None}[library_clip])
    got = run(lib, cand("a/IMG_1.JPG", photo), cand("a/IMG_1.MOV", clip, live_of="a/IMG_1.JPG"))
    if library_clip == "the same":
        assert got == summary(already_in_library=2)
    elif library_clip == "another":  # the item keeps the clip it has
        assert got == summary(already_in_library=1, problems=[{"source_path": "a/IMG_1.MOV",
                                                               "reason": "clip not in library"}])
    else:  # an item cannot take a clip later, so the clip's bytes are kept as a video of their own
        assert got == summary(imported=1, already_in_library=1, bytes=len(clip), problems=[
            {"source_path": "a/IMG_1.MOV", "reason": "clip kept as its own item"}])
        assert [f["name"] for f in fold(read_all(lib)).items[sha(clip)].files] == ["IMG_1.MOV"]
        assert verify(lib).ok


@needs_exiftool
def test_clip_of_another_copy_counts_by_its_own_bytes(lib):
    photo, clip, other = exif_jpeg(), b"live clip", b"another clip"
    got = run(lib, cand("x/IMG_1.JPG", photo),
              cand("y/IMG_1.JPG", photo), cand("y/IMG_1.MP4", clip, live_of="y/IMG_1.JPG"),
              cand("z/IMG_1.JPG", photo), cand("z/IMG_1.MP4", clip, live_of="z/IMG_1.JPG"),
              cand("w/IMG_1.JPG", photo), cand("w/IMG_1.MP4", other, live_of="w/IMG_1.JPG"))
    # x has no clip, so y's is the item's: a clip only one copy has is not lost
    assert got == summary(imported=1, live_clips=1, duplicates_in_source=4, bytes=len(photo) + len(clip),
                          problems=[{"source_path": "w/IMG_1.MP4", "reason": "clip not in library"}])
    assert {f["role"]: f["sha256"] for f in fold(read_all(lib)).items[sha(photo)].files}["live"] == sha(clip)


@needs_exiftool
def test_clip_whose_bytes_were_deleted_is_left_out(lib):
    photo, clip = exif_jpeg(), b"live clip"
    events.append(lib, [imp(sha(clip), name="IMG_1.MP4", copy=lib.copy_id), ev("trash", sha(clip), ts=T1,
                                                                               copy=lib.copy_id)])
    got = run(lib, cand("a/IMG_1.JPG", photo), cand("a/IMG_1.MP4", clip, live_of="a/IMG_1.JPG"))
    assert got == summary(imported=1, skipped_deleted=1, bytes=len(photo))
    assert [f["role"] for f in fold(read_all(lib)).items[sha(photo)].files] == ["main"]


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
    item = fold(read_all(lib)).items[sha(b)]
    assert (item.status, item.version_of) == ("live", sha(a))
    assert verify(lib).ok


@needs_exiftool
def test_unreadable_file_is_imported_by_a_later_run(lib):
    good, flaky = exif_jpeg((1, 2, 3)), exif_jpeg((9, 9, 9))
    first = run(lib, cand("x/1.JPG", good), dataclasses.replace(cand("x/2.JPG", flaky), read=unplugged),
                dataclasses.replace(cand("x/3.JPG", b"xx"), size=5))
    assert first == summary(imported=1, bytes=len(good), problems=[
        {"source_path": "x/2.JPG", "reason": "could not be read: source unplugged"},
        {"source_path": "x/3.JPG", "reason": "2 bytes read, 5 listed"}])
    (earlier,) = lib.sources.iterdir()
    assert [f["source_path"] for f in manifest(lib)["files"]] == ["x/1.JPG"]  # nothing unread recorded as final
    repaired = [cand("x/1.JPG", good), cand("x/2.JPG", flaky), cand("x/3.JPG", b"xxxxx")]
    # flaky shares good's fingerprint: another version of it
    assert run(lib, *repaired) == summary(imported=2, versions=1, already_in_library=1, bytes=len(flaky) + 5)
    (later,) = set(lib.sources.iterdir()) - {earlier}
    assert fold(read_all(lib)).items[sha(flaky)].imports == [later.name]
    assert verify(lib).ok
    log = read_all(lib)
    assert run(lib, *repaired) == summary(already_in_library=3)
    assert read_all(lib) == log and len(list(lib.sources.iterdir())) == 2


@needs_exiftool
def test_photo_with_an_unreadable_clip_is_imported_alone(lib):
    photo, clip = exif_jpeg(), b"live clip"
    pair = [cand("a/IMG_1.JPG", photo), cand("a/IMG_1.MP4", clip, live_of="a/IMG_1.JPG")]
    got = run(lib, pair[0], dataclasses.replace(pair[1], read=unplugged))
    assert got == summary(imported=1, bytes=len(photo), problems=[
        {"source_path": "a/IMG_1.MP4", "reason": "could not be read: source unplugged; photo imported without it"}])
    assert [f["role"] for f in fold(read_all(lib)).items[sha(photo)].files] == ["main"]
    # repaired later: the photo's item cannot take it any more, so the clip is kept as an item of its own
    assert run(lib, *pair) == summary(imported=1, already_in_library=1, bytes=len(clip), problems=[
        {"source_path": "a/IMG_1.MP4", "reason": "clip kept as its own item"}])
    assert verify(lib).ok


@needs_exiftool
def test_clip_of_an_unreadable_photo_is_kept_as_its_own_item(lib):
    photo, clip = exif_jpeg(), b"live clip"
    got = run(lib, dataclasses.replace(cand("a/IMG_1.JPG", photo), read=unplugged),
              cand("a/IMG_1.MP4", clip, live_of="a/IMG_1.JPG"))
    assert got == summary(imported=1, bytes=len(clip), problems=[
        {"source_path": "a/IMG_1.JPG", "reason": "could not be read: source unplugged"},
        {"source_path": "a/IMG_1.MP4", "reason": "clip kept as its own item"}])
    assert sha(clip) in fold(read_all(lib)).items


@needs_exiftool
def test_edit_of_an_unreadable_original_is_imported_alone(lib):
    edit = exif_jpeg((9, 9, 9))
    got = run(lib, dataclasses.replace(cand("a/IMG_1.JPG", exif_jpeg()), read=unplugged),
              cand("a/IMG_1-edited.JPG", edit, edit_of="a/IMG_1.JPG"))
    assert got == summary(imported=1, bytes=len(edit), problems=[
        {"source_path": "a/IMG_1.JPG",
         "reason": "could not be read: source unplugged; its edit imported without the link"}])
    assert fold(read_all(lib)).items[sha(edit)].edit_of is None


@needs_exiftool
@pytest.mark.parametrize("order", ["year folder first", "album first"])
def test_copy_read_by_a_later_run_brings_its_album(lib, order):
    photo = exif_jpeg()
    year, album = cand("Photos from 2024/IMG_1.JPG", photo), cand("Trip/IMG_1.JPG", photo, albums=("Trip",))
    broken = dataclasses.replace(album, read=unplugged)
    first, later = ([year, broken], [year, album]) if order == "year folder first" else ([broken, year], [album, year])
    run(lib, *first)
    got = run(lib, *later)
    assert (got["already_in_library"] + got["duplicates_in_source"], got["album_links"]) == (2, 1)
    assert fold(read_all(lib)).items[sha(photo)].albums == {("album", "Trip")}
    log = read_all(lib)
    assert run(lib, *later)["album_links"] == 0 and read_all(lib) == log
    folders = sorted(lib.sources.iterdir())
    paths = [[(f["source_path"], f["path"]) for f in json.loads((d / "manifest.json").read_bytes())["files"]]
             for d in folders]
    assert sorted(paths) == [[("Photos from 2024/IMG_1.JPG", "originals/2024/03/IMG_1.JPG")],
                             [("Trip/IMG_1.JPG", "originals/2024/03/IMG_1.JPG")]]


@needs_exiftool
@pytest.mark.parametrize("owner", ["unfavorite", "album.remove", "album.rename"])
def test_a_later_source_never_undoes_the_owners_marks(lib, owner):
    photo, other = exif_jpeg((1, 2, 3)), exif_jpeg((9, 9, 9), model="Other")
    marked = cand("Trip/IMG_1.JPG", photo, favorite=True, albums=("Trip",))
    run(lib, marked)
    item = sha(photo)
    change = {"unfavorite": {"item": item}, "album.remove": {"item": item, "kind": "album", "album": "Trip"},
              "album.rename": {"kind": "album", "album": "Trip", "to": "Trip 2024"}}[owner]
    events.append(lib, [events.new_event(lib, owner, **change)])
    before = fold(read_all(lib)).items[item]
    got = run(lib, marked, cand("Trip/IMG_2.JPG", other))  # another listing, so another import id
    after = fold(read_all(lib)).items[item]
    assert (got["already_in_library"], got["favorites"], got["album_links"]) == (1, 0, 0)
    assert (after.favorite, after.albums) == (before.favorite, before.albums)
    assert len(after.imports) == 2


@needs_exiftool
def test_a_later_source_adds_marks_the_item_never_had(lib):
    photo, other = exif_jpeg((1, 2, 3)), exif_jpeg((9, 9, 9), model="Other")
    run(lib, cand("a/IMG_1.JPG", photo))
    got = run(lib, cand("Trip/IMG_1.JPG", photo, favorite=True, albums=("Trip",)), cand("a/IMG_2.JPG", other))
    assert (got["favorites"], got["album_links"]) == (1, 1)
    item = fold(read_all(lib)).items[sha(photo)]
    assert (item.favorite, item.albums) == (True, {("album", "Trip")})


@needs_exiftool
def test_unreadable_metadata_file_stops_the_run_before_any_write(lib):
    before = snapshot(lib)
    with pytest.raises(ImportFailed, match=r"^a/x\.json could not be read \(source unplugged\); nothing was imported$"):
        run(lib, cand("a/x.jpg", exif_jpeg()), sources=(SourceFile("a/x.json", unplugged),))
    assert snapshot(lib) == before


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
def test_dry_run_that_fails_leaves_no_staging(lib):
    put(lib, "originals/2024/03/IMG_1.JPG", b"dropped in by hand")
    with pytest.raises(ImportFailed):
        run(lib, cand("a/IMG_1.JPG", exif_jpeg()), dry_run=True)
    assert staged(lib) == []


@needs_exiftool
def test_dry_run_keeps_the_staging_a_cut_off_run_left(lib, monkeypatch):
    cands = [cand("x/IMG_1.JPG", exif_jpeg())]

    def cut_off(*_):
        raise OSError("power lost")

    monkeypatch.setattr(staging.Stage, "exif", cut_off)
    with pytest.raises(OSError, match="power lost"):
        run(lib, *cands)
    monkeypatch.undo()
    left = staged(lib)
    assert run(lib, *cands, dry_run=True)["imported"] == 1
    assert staged(lib) == left
    assert run(lib, dataclasses.replace(cands[0], read=unplugged))["imported"] == 1  # what was staged is used


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
    assert got == summary(imported=2, versions=1, album_links=1, bytes=len(a) + len(b))
    (folder,) = lib.sources.iterdir()
    imports = [e for e in read_all(lib) if e["op"] == "import"]
    assert sorted(e["item"] for e in imports) == sorted([sha(a), sha(b)])
    assert {e["import"] for e in imports} == {folder.name}
    assert [e["op"] for e in read_all(lib)].count("album.add") == 1
    assert verify(lib).ok and staged(lib) == []


@needs_exiftool
def test_run_cut_off_after_the_log_refreshes_the_index_next_time(lib, monkeypatch):
    data = exif_jpeg()
    real = Cache.refresh

    def cut_off(self, **_):
        monkeypatch.setattr(Cache, "refresh", real)
        raise OSError("power lost")

    monkeypatch.setattr(Cache, "refresh", cut_off)
    with pytest.raises(OSError, match="power lost"):
        run(lib, cand("a/IMG_1.JPG", data))
    assert Cache(lib).items_json() == []
    assert run(lib, cand("a/IMG_1.JPG", data)) == summary(already_in_library=1)
    assert [r["id"] for r in Cache(lib).items_json()] == [sha(data)]
    assert staged(lib) == []


@needs_exiftool
def test_folders_are_flushed_before_the_log_names_their_files(lib, monkeypatch):
    order: list = []
    real_flush, real_append = fsops.fsync_dir, events.append

    def flush(folder):
        order.append(folder)
        real_flush(folder)

    def append(*args):
        order.append("log")
        real_append(*args)

    monkeypatch.setattr(fsops, "fsync_dir", flush)
    monkeypatch.setattr(events, "append", append)
    run(lib, cand("a/IMG_1.JPG", exif_jpeg()))
    assert order.index(lib.originals / "2024/03") < order.index("log")
    assert order.index(lib.originals / "2024") < order.index("log")
    assert order.index(lib.sources) < order.index("log")


@needs_exiftool
def test_exiftool_reads_every_staged_file_whatever_its_name(lib):
    hidden, odd = exif_jpeg((1, 2, 3)), exif_jpeg((9, 9, 9))
    run(lib, cand(".hidden/IMG_1.JPG", hidden), cand("a/IMG_2.dat", odd))
    state = fold(read_all(lib))
    assert state.items[sha(hidden)].fingerprint == tuple(FP)
    assert state.items[sha(odd)].folder == "2024/03"


@needs_exiftool
def test_staged_files_keep_the_source_date(lib):
    data = exif_jpeg()
    run(lib, cand("a/IMG_1.JPG", data, mtime="2021-05-06T12:00:00Z"))
    (folder,) = lib.sources.iterdir()
    (record,) = json.loads((folder / "exif.json").read_bytes())
    assert record["FileModifyDate"].startswith("2021:05:06")
    assert (lib.originals / "2024/03/IMG_1.JPG").stat().st_mtime == datetime(2021, 5, 6, 12, tzinfo=UTC).timestamp()


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
    assert got == summary(imported=2, versions=1, bytes=len(a) + len(b))
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
                 [SourceFile("x/IMG_1.JPG.json", lambda: b"{}")], False, datetime.now(UTC), roots=())
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


@pytest.mark.parametrize("path", ["../x.jpg", "/x.jpg", "a/./x.jpg", "a//x.jpg", "a/", "C:x.jpg", "a\\..\\x.jpg",
                                  "a/x\n.jpg"])
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
    before = snapshot(lib)
    with pytest.raises(ImportFailed, match="overlap"):
        run(lib, cand("a/x.jpg", b"x"), roots=(tmp_path / "elsewhere", source))
    assert snapshot(lib) == before


def test_adapter_notes_are_listed_once_for_files_this_import_records():
    """A note reaches problems only for a file this import writes an event for, on that file's one row: not for one
    an earlier run recorded, a duplicate, a deleted photo's copy or a file that could not be read."""
    this = "2026-11-27-test-bbbbbb"
    state = fold([imp(sha(b"held")), imp(sha(b"settled"), import_id=this), imp(sha(b"deleted")),
                  ev("trash", sha(b"deleted"))])
    cands = [cand(path, data, note="no sidecar", **marks) for path, data, marks in (
        ("a/new.jpg", b"new", {}), ("a/new.MOV", b"clip", {"live_of": "a/new.jpg"}), ("a/copy.jpg", b"new", {}),
        ("a/held.jpg", b"held", {}), ("a/settled.jpg", b"settled", {}), ("a/deleted.jpg", b"deleted", {}),
        ("a/gone.jpg", b"gone", {}), ("a/IMG_9.JPG", b"photo", {}),
        ("a/IMG_9.MOV", b"clip 9", {"live_of": "a/IMG_9.JPG"}))]
    unread = dict.fromkeys(("a/gone.jpg", "a/IMG_9.JPG"), "could not be read: gone")
    shas = {c.source_path: sha(c.read().read()) for c in cands if c.source_path not in unread}
    problems: list[dict] = []
    importplan.plan(state, cands, shas, {}, dict.fromkeys(shas, this), importplan.clips_of(cands), unread, problems)
    assert sorted((r["source_path"], r["reason"]) for r in problems) == [
        ("a/IMG_9.JPG", "could not be read: gone"), ("a/IMG_9.MOV", "clip kept as its own item; no sidecar"),
        ("a/gone.jpg", "could not be read: gone"), ("a/held.jpg", "no sidecar"), ("a/new.MOV", "no sidecar"),
        ("a/new.jpg", "no sidecar")]

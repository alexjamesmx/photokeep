"""Google Takeout: sidecar pairing, the listing of zips and extracted folders, provider records, and an import."""
import dataclasses
import json
import shutil
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from photokeep import importer
from photokeep.adapters import takeout
from photokeep.cache import Cache
from photokeep.events import read_all
from photokeep.importer import ImportFailed
from photokeep.metadata import provider_record
from photokeep.providers import takeout as provider
from photokeep.state import fold
from tests.helpers import exif_jpeg, jpeg_bytes, sha

needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool is not installed")
GP = "Google Photos"
Y = f"{GP}/Photos from 2024"
TRIP = f"{GP}/Trip"
LONG = "PXL_20230806_181530412.PORTRAIT.jpg"  # its whole sidecar name would pass 51 characters
WIDE = "Screenshot_20230815-142536_com.android.chrome_"  # 46 characters: names after it share one cut sidecar


def cut(name: str) -> str:
    """The sidecar name Google writes for a file whose whole sidecar name passes 51 characters."""
    return f"{name}.supplemental-metadata"[:46] + ".json"


def sidecar(title: str, taken: int = 1709632800, geo: tuple = (0.0, 0.0), geo_exif: tuple | None = None,
            **more) -> bytes:
    """A sidecar as Google writes it; 0.0/0.0 is its way of saying no place."""
    def place(lat: float, lon: float) -> dict:
        return {"latitude": lat, "longitude": lon, "altitude": 0.0, "latitudeSpan": 0.0, "longitudeSpan": 0.0}

    body = {"title": title, "description": "", "imageViews": "3",
            "creationTime": {"timestamp": "1732000000", "formatted": "19 Nov 2024, 07:06:40 UTC"},
            "photoTakenTime": {"timestamp": str(taken), "formatted": "a date"}, "geoData": place(*geo),
            **({"geoDataExif": place(*geo_exif)} if geo_exif else {}),
            "url": "https://photos.google.com/photo/x", "googlePhotosOrigin": {"mobileUpload": {}}, **more}
    return json.dumps(body, indent=2).encode()


@dataclass
class Export:
    parts: list[Path]
    media: dict[str, bytes]  # source path -> bytes
    sidecars: dict[str, bytes]  # every JSON file, by source path
    pairs: dict[str, str]  # media -> the sidecar it should get


def zip_part(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for rel, data in files.items():
            zf.writestr(f"Takeout/{rel}", data)
    return path


def export(tmp_path: Path) -> Export:
    """A Takeout in two parts. Part 2 holds one year-folder sidecar and the album's metadata.json; the album Trip
    holds a copy of a year-folder photo. Sidecars come in every name Google has used."""
    first = exif_jpeg((10, 20, 30))
    names = ["IMG_0002.jpg", "IMG_0002(1).jpg", LONG, "OLD_1.jpg", "OLD_2.jpg", "IMG_0003.jpg",
             "IMG_0003-edited.jpg", "IMG_0004.jpg", "IMG_0005.jpg", "IMG_0006.jpg", "LONE.jpg"]
    media = {f"{Y}/IMG_0001.jpg": first, **{f"{Y}/{n}": jpeg_bytes((20 * k + 20, 0, 0)) for k, n in enumerate(names)},
             f"{Y}/IMG_0004.MP4": b"live clip", f"{TRIP}/IMG_0001.jpg": first}
    plain = f"{Y}/IMG_0001.jpg.supplemental-metadata.json"
    sidecars = {
        f"{Y}/IMG_0002.jpg.supplemental-metadata.json": sidecar("IMG_0002.jpg", taken=1700000000),
        f"{Y}/IMG_0002.jpg.supplemental-metadata(1).json": sidecar("IMG_0002.jpg"),
        f"{Y}/{cut(LONG)}": sidecar(LONG),
        f"{Y}/OLD_1.jpg.json": sidecar("OLD_1.heic"),
        f"{Y}/OLD_2.json": sidecar("OLD_2.jpg"),
        f"{Y}/IMG_0003.jpg.supplemental-metadata.json": sidecar("IMG_0003.jpg", description="Birthday"),
        f"{Y}/IMG_0004.jpg.supplemental-metadata.json": sidecar("IMG_0004.jpg"),
        f"{Y}/IMG_0004.MP4.supplemental-metadata.json": sidecar("IMG_0004.mp4"),
        f"{Y}/IMG_0005.jpg.supplemental-metadata.json": sidecar("IMG_0005.jpg", trashed=True),
        f"{Y}/IMG_0006.jpg.supplemental-metadata.json": sidecar("IMG_0006.jpg", geo_exif=(19.43, -99.13),
                                                                favorited=True),
        f"{TRIP}/IMG_0001.jpg.supplemental-metadata.json": sidecar("IMG_0001.jpg"),
    }
    later = {plain: sidecar("IMG_0001.jpg"), f"{TRIP}/metadata.json": json.dumps({"title": "Summer trip"}).encode()}
    pairs = {f"{Y}/IMG_0001.jpg": plain, f"{Y}/IMG_0002.jpg": f"{Y}/IMG_0002.jpg.supplemental-metadata.json",
             f"{Y}/IMG_0002(1).jpg": f"{Y}/IMG_0002.jpg.supplemental-metadata(1).json",
             f"{Y}/{LONG}": f"{Y}/{cut(LONG)}", f"{Y}/OLD_1.jpg": f"{Y}/OLD_1.jpg.json",
             f"{Y}/OLD_2.jpg": f"{Y}/OLD_2.json",
             **{f"{Y}/{n}": f"{Y}/{n}.supplemental-metadata.json"
                for n in ("IMG_0003.jpg", "IMG_0004.jpg", "IMG_0004.MP4", "IMG_0005.jpg", "IMG_0006.jpg")},
             f"{Y}/IMG_0003-edited.jpg": f"{Y}/IMG_0003.jpg.supplemental-metadata.json",
             f"{TRIP}/IMG_0001.jpg": f"{TRIP}/IMG_0001.jpg.supplemental-metadata.json"}
    parts = [zip_part(tmp_path / "takeout-001.zip", media | sidecars), zip_part(tmp_path / "takeout-002.zip", later)]
    return Export(parts, media, sidecars | later, pairs)


def pairs(media: list[str], sidecars: list[str]) -> dict[str, str]:
    """pair() on file names in one folder, by name."""
    got = provider.pair([f"{Y}/{m}" for m in media], [f"{Y}/{s}" for s in sidecars])
    return {PurePosixPath(m).name: PurePosixPath(s).name for m, s in got.items()}


def test_exact_sidecar_name():
    assert pairs(["IMG_1.jpg", "IMG_2.jpg"], ["IMG_1.jpg.supplemental-metadata.json"]) == {
        "IMG_1.jpg": "IMG_1.jpg.supplemental-metadata.json"}


def test_numbered_copy_takes_the_numbered_sidecar():
    numbered = "IMG_1.jpg.supplemental-metadata(1).json"
    assert pairs(["IMG_1.jpg", "IMG_1(1).jpg"], ["IMG_1.jpg.supplemental-metadata.json", numbered]) == {
        "IMG_1.jpg": "IMG_1.jpg.supplemental-metadata.json", "IMG_1(1).jpg": numbered}


@pytest.mark.parametrize("name", [LONG, WIDE + "1.jpg"])  # cut inside the suffix, or inside the name
def test_sidecar_name_cut_at_51_characters(name):
    assert len(cut(name)) == 51
    assert pairs([name], [cut(name)]) == {name: cut(name)}


@pytest.mark.parametrize("name", ["IMG_1.jpg.json", "IMG_1.json"])
def test_older_sidecar_names(name):
    assert pairs(["IMG_1.jpg"], [name]) == {"IMG_1.jpg": name}


def test_each_sidecar_name_comes_before_the_next():
    name = "IMG_20230101_123456(1).jpg"
    forms = [f"{name}.supplemental-metadata.json", "IMG_20230101_123456.jpg.supplemental-metadata(1).json",
             cut(name), f"{name}.json", "IMG_20230101_123456(1).json"]
    assert len(set(forms)) == 5
    for n in range(5):
        assert pairs([name], forms[n:]) == {name: forms[n]}


def test_a_sidecar_two_files_could_take_goes_to_neither():
    assert pairs(["IMG_1.jpg", "IMG_1.mp4"], ["IMG_1.json"]) == {}
    a, b = WIDE + "1.jpg", WIDE + "2.jpg"
    assert cut(a) == cut(b)
    assert pairs([a, b], [cut(a)]) == {}


def test_edited_copy_shares_its_originals_sidecar():
    one, two, three = (f"IMG_{n}.jpg.supplemental-metadata.json" for n in (1, 2, 3))
    got = pairs(["IMG_1.jpg", "IMG_1-edited.jpg", "IMG_2.jpg", "IMG_2-EDITED.jpg", "IMG_3-edited.jpg",
                 "IMG_4-edited.jpg"], [one, two, three])
    assert got == {"IMG_1.jpg": one, "IMG_1-edited.jpg": one, "IMG_2.jpg": two, "IMG_2-EDITED.jpg": two,
                   "IMG_3-edited.jpg": three}  # an original need not be in the listing for its sidecar to count


def test_edited_copy_never_takes_or_blocks_its_originals_cut_sidecar():
    original, edited = WIDE + ".jpg", WIDE + "-edited.jpg"
    assert cut(original) == cut(edited)
    assert pairs([original, edited], [cut(original)]) == {original: cut(original), edited: cut(original)}


def test_edited_copy_keeps_a_sidecar_of_its_own():
    own = "IMG_1-edited.jpg.supplemental-metadata.json"
    assert pairs(["IMG_1.jpg", "IMG_1-edited.jpg"], ["IMG_1.jpg.supplemental-metadata.json", own]) == {
        "IMG_1.jpg": "IMG_1.jpg.supplemental-metadata.json", "IMG_1-edited.jpg": own}


def test_sidecars_pair_only_within_their_folder():
    got = provider.pair([f"{TRIP}/IMG_1.jpg", f"{Y}/IMG_1.jpg"], [f"{Y}/IMG_1.jpg.supplemental-metadata.json"])
    assert got == {f"{Y}/IMG_1.jpg": f"{Y}/IMG_1.jpg.supplemental-metadata.json"}


def test_album_metadata_is_no_sidecar():
    assert pairs(["metadata.jpg"], ["metadata.json"]) == {}


def test_record_of_a_sidecar():
    data = sidecar("IMG_1.jpg", taken=1700000000, geo=(19.4326, -99.1332), description="At the market",
                   favorited=True)
    assert provider.record(data) == provider_record(taken="2023-11-14T22:13:20Z", lat=19.4326, lon=-99.1332,
                                                    title="IMG_1.jpg", description="At the market", favorite=True,
                                                    raw=json.loads(data))


@pytest.mark.parametrize("geo, geo_exif, place", [((0.0, 0.0), (19.43, -99.13), (19.43, -99.13)),
                                                  ((0.0, 0.0), (0.0, 0.0), (None, None)),
                                                  ((0.0, 0.0), None, (None, None)),
                                                  ((1.5, 2.5), (19.43, -99.13), (1.5, 2.5))])
def test_place_is_googles_then_the_files_and_zero_is_none(geo, geo_exif, place):
    got = provider.record(sidecar("IMG_1.jpg", geo=geo, geo_exif=geo_exif))
    assert (got["lat"], got["lon"]) == place


def test_record_of_odd_fields_keeps_what_it_can():
    raw = {"title": 5, "description": "", "photoTakenTime": {"timestamp": "soon"},
           "geoData": {"latitude": "19", "longitude": -99.0}, "favorited": "yes"}
    assert provider.record(json.dumps(raw).encode()) == provider_record(raw=raw)


@pytest.mark.parametrize("data", [b"not json", b"[1]", b'"text"', b"\xff\xfe\x00"])
def test_sidecar_that_is_not_a_json_object_gives_no_record(data):
    assert provider.record(data) == {}


def test_listing_pairs_every_file_across_parts(tmp_path):
    e = export(tmp_path)
    cands, sources = takeout.list(e.parts)
    assert [c.source_path for c in cands] == sorted(p for p in e.media if p.startswith(Y)) + [f"{TRIP}/IMG_0001.jpg"]
    by = {c.source_path: c for c in cands}
    assert {p: c.provider for p, c in by.items() if c.provider} == {
        m: provider.record(e.sidecars[s]) for m, s in e.pairs.items()}
    assert [by[f"{Y}/{n}"].name for n in ("IMG_0002(1).jpg", "OLD_1.jpg", "IMG_0004.MP4", "IMG_0003-edited.jpg",
                                         "LONE.jpg")] == ["IMG_0002.jpg", "OLD_1.jpg", "IMG_0004.mp4",
                                                          "IMG_0003-edited.jpg", "LONE.jpg"]
    assert {p: c.albums for p, c in by.items() if c.albums} == {f"{TRIP}/IMG_0001.jpg": ("Summer trip",)}
    assert {p: c.live_of for p, c in by.items() if c.live_of} == {f"{Y}/IMG_0004.MP4": f"{Y}/IMG_0004.jpg"}
    assert {p: c.edit_of for p, c in by.items() if c.edit_of} == {f"{Y}/IMG_0003-edited.jpg": f"{Y}/IMG_0003.jpg"}
    assert [p for p, c in by.items() if c.trashed] == [f"{Y}/IMG_0005.jpg"]
    assert [p for p, c in by.items() if c.favorite] == [f"{Y}/IMG_0006.jpg"]
    assert {p: c.note for p, c in by.items() if c.note} == {f"{Y}/LONE.jpg": "no sidecar"}
    for c in cands:  # read in place from the zip, long after the listing
        with c.read() as f:
            assert (f.read(), c.size, c.mtime) == (e.media[c.source_path], len(e.media[c.source_path]), None)
    assert [(s.path, s.read()) for s in sources] == sorted(e.sidecars.items())


def test_records_read_the_sidecar(tmp_path):
    by = {c.source_path: c.provider for c in takeout.list(export(tmp_path).parts)[0]}
    assert by[f"{Y}/IMG_0002.jpg"]["taken"] == "2023-11-14T22:13:20Z"
    assert (by[f"{Y}/IMG_0006.jpg"]["lat"], by[f"{Y}/IMG_0006.jpg"]["lon"]) == (19.43, -99.13)
    assert (by[f"{Y}/IMG_0001.jpg"]["lat"], by[f"{Y}/IMG_0001.jpg"]["lon"]) == (None, None)
    assert by[f"{Y}/IMG_0003.jpg"]["description"] == "Birthday"
    assert by[f"{Y}/IMG_0003-edited.jpg"] == by[f"{Y}/IMG_0003.jpg"]


def test_album_without_metadata_is_named_by_its_folder(tmp_path):
    part = zip_part(tmp_path / "t.zip", {f"{TRIP}/IMG_1.jpg": b"x",
                                         f"{TRIP}/IMG_1.jpg.supplemental-metadata.json": sidecar("IMG_1.jpg")})
    (c,) = takeout.list([part])[0]
    assert c.albums == ("Trip",)


def test_extracted_parts_list_as_their_zips_do(tmp_path):
    e = export(tmp_path)
    folders = []
    for n, part in enumerate(e.parts):
        with zipfile.ZipFile(part) as zf:
            zf.extractall(tmp_path / f"extracted-{n}")
        folders.append(tmp_path / f"extracted-{n}")

    def listing(sources: list[Path]) -> tuple[list, list]:
        cands, kept = takeout.list(sources)
        return [dataclasses.replace(c, read=None) for c in cands], [(s.path, s.read()) for s in kept]

    assert listing(folders) == listing([e.parts[0], folders[1]]) == listing(e.parts)
    first = takeout.list(folders)[0][0]
    with first.read() as f:
        assert f.read() == e.media[first.source_path]


def test_refuses_what_is_no_takeout(tmp_path):
    other = zip_part(tmp_path / "drive.zip", {"Drive/notes.txt": b"x"})
    with pytest.raises(ImportFailed, match="holds no Takeout/Google Photos/"):
        takeout.list([other])
    (tmp_path / "bad.zip").write_bytes(b"not a zip")
    with pytest.raises(ImportFailed, match="bad.zip cannot be read as a zip"):
        takeout.list([tmp_path / "bad.zip"])
    (tmp_path / "empty").mkdir()
    with pytest.raises(ImportFailed, match="holds no Takeout/Google Photos/"):
        takeout.list([tmp_path / "empty"])


def test_refuses_a_file_in_two_sources(tmp_path):
    part = export(tmp_path).parts[0]
    with pytest.raises(ImportFailed, match="is in both"):
        takeout.list([part, part])


def write_import_folder(folder: Path, e: Export, sources, paths: list[str]) -> None:
    """sources/<import id>/ as an import that recorded these media paths keeps it."""
    for s in sources:
        (folder / s.path).parent.mkdir(parents=True, exist_ok=True)
        (folder / s.path).write_bytes(s.read())
    files = [{"source_path": p, "sha256": sha(e.media[p]), "size": len(e.media[p]), "mtime": None, "path": None}
             for p in paths]
    (folder / "manifest.json").write_text(json.dumps({"import": folder.name, "adapter": "takeout", "files": files}))


def test_provider_reader_gives_each_file_the_record_the_listing_gave(tmp_path):
    e = export(tmp_path)
    cands, sources = takeout.list(e.parts)
    folder = tmp_path / "2026-11-27-takeout-abc123"
    write_import_folder(folder, e, sources, [c.source_path for c in cands])
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["files"][0]["sha256"] = None  # a file the import could not read
    (folder / "manifest.json").write_text(json.dumps(manifest))
    assert provider.read(folder) == {c.source_path: c.provider for c in cands if c.provider}


def test_provider_reader_pairs_an_edit_whose_original_another_import_recorded(tmp_path):
    e = export(tmp_path)
    cands, sources = takeout.list(e.parts)
    folder = tmp_path / "2026-11-27-takeout-def456"
    write_import_folder(folder, e, sources, [f"{Y}/IMG_0003-edited.jpg"])
    original = next(c for c in cands if c.source_path == f"{Y}/IMG_0003.jpg")
    assert provider.read(folder) == {f"{Y}/IMG_0003-edited.jpg": original.provider}


@needs_exiftool
def test_takeout_import(lib, tmp_path, mexico):
    e = export(tmp_path)
    cands, sources = takeout.list(e.parts)
    got = importer.run(lib, "takeout", cands, sources, False, datetime.now(UTC), roots=e.parts)
    assert got == {"imported": 12, "versions": 0, "edits": 1, "live_clips": 1, "already_in_library": 0,
                   "duplicates_in_source": 1, "skipped_deleted": 0, "trashed": 1, "album_links": 1, "favorites": 1,
                   "bytes": sum(len(d) for p, d in e.media.items() if p.startswith(Y)),
                   "problems": [{"source_path": f"{Y}/LONE.jpg", "reason": "no sidecar"}]}
    state = fold(read_all(lib))
    item = {p: state.items.get(sha(d)) for p, d in e.media.items()}
    assert len(state.items) == 12
    assert item[f"{Y}/IMG_0001.jpg"].albums == {("album", "Summer trip")}
    assert item[f"{Y}/IMG_0003-edited.jpg"].edit_of == sha(e.media[f"{Y}/IMG_0003.jpg"])
    assert [f["sha256"] for f in item[f"{Y}/IMG_0004.jpg"].files if f["role"] == "live"] == [sha(b"live clip")]
    assert [p for p, i in item.items() if i and i.status == "trashed"] == [f"{Y}/IMG_0005.jpg"]
    assert item[f"{Y}/IMG_0006.jpg"].favorite
    # no EXIF date: the sidecar's, shown in local time, files it
    assert (lib.originals / "2023/11/IMG_0002.jpg").read_bytes() == e.media[f"{Y}/IMG_0002.jpg"]
    row = next(r for r in Cache(lib).items_json() if r["id"] == sha(e.media[f"{Y}/IMG_0002.jpg"]))
    assert (row["t"], row["ds"]) == ("2023-11-14T16:13:20", "provider")
    (folder,) = lib.sources.iterdir()
    assert all((folder / path).read_bytes() == data for path, data in e.sidecars.items())
    where = {f["source_path"]: f["path"] for f in json.loads((folder / "manifest.json").read_bytes())["files"]}
    assert sorted(where) == sorted(e.media)  # the album's copy too, at the one item's path
    assert where[f"{TRIP}/IMG_0001.jpg"] == where[f"{Y}/IMG_0001.jpg"] == "originals/2024/03/IMG_0001.jpg"
    assert provider.read(folder) == {c.source_path: c.provider for c in cands if c.provider}

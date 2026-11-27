import json
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from photokeep import cache as cache_module
from photokeep import events, media
from photokeep.cache import Cache
from photokeep.cli import main
from photokeep.events import ts_str
from photokeep.hashcache import HashCache
from photokeep.library import LibraryError
from tests.helpers import DETAILS_HEADER, T2, add_item, add_source, ev, imp, jpeg_bytes, mark, put, render_or_die, sha

IMPORT = "2026-01-05-icloud-a1b2c3"
KEYS = {"id", "n", "dir", "k", "sc", "s", "w", "h", "d", "c", "lc", "lv", "t", "z", "ds", "cam", "la", "lo", "f",
        "hi", "al", "me", "o", "sh", "br", "no", "g", "b", "th", "pv", "tr", "ta", "r", "left"}
needs_media = pytest.mark.skipif(not media.available()[0], reason="Pillow, numpy, heif-dec or ffmpeg missing")


def icloud_import(lib):
    """Two photos of one iCloud import: A has EXIF; B has none, so its date comes from Photo Details."""
    a = add_item(lib, b"photo a", name="IMG_1.JPG", import_id=IMPORT)
    b = add_item(lib, b"photo b", name="IMG_2.JPG", import_id=IMPORT)
    add_source(lib, IMPORT, {"part1/Photos/IMG_1.JPG": b"photo a", "part1/Photos/IMG_2.JPG": b"photo b"},
               exif=[{"SourceFile": "raw/part1/Photos/IMG_1.JPG", "DateTimeOriginal": "2024:01:03 08:00:00",
                      "OffsetTimeOriginal": "-06:00", "Make": "Apple", "Model": "iPhone 15",
                      "ImageWidth": 4032, "ImageHeight": 3024},
                     {"SourceFile": "part1/Photos/IMG_2.JPG", "MIMEType": "image/jpeg"}],
               dates={"IMG_1.JPG": "Monday January 1,2024 9:00 AM GMT",
                      "IMG_2.JPG": "Thursday January 4,2024 5:34 PM GMT"})
    return a, b


def refreshed(lib) -> Cache:
    cache = Cache(lib)
    cache.refresh(render=False)
    return cache


def seed_media(lib, rows: dict[str, dict]) -> None:
    """Media rows as a render (or the migration) leaves them."""
    with closing(sqlite3.connect(lib.cache / "index.sqlite")) as db, db:
        for item, cols in rows.items():
            db.execute(f"INSERT OR REPLACE INTO media (id, {', '.join(cols)}) VALUES (?{', ?' * len(cols)})",
                       (item, *cols.values()))


def test_file_without_exif_takes_the_provider_date(lib, mexico):
    a, b = icloud_import(lib)
    x, y = (refreshed(lib).item_json(i) for i in (a, b))
    assert (x["t"], x["z"], x["ds"]) == ("2024-01-03T08:00:00", "-06:00", "exif")
    assert (y["t"], y["z"], y["ds"]) == ("2024-01-04T11:34:00", "-06:00", "provider")
    assert (x["cam"], x["w"], x["h"], x["o"]) == ("Apple iPhone 15", 4032, 3024, "part1/Photos/IMG_1.JPG")
    assert (y["cam"], y["w"], y["o"]) == (None, None, "part1/Photos/IMG_2.JPG")


def test_item_json_has_the_viewer_keys(lib):
    a, b = icloud_import(lib)
    cache = refreshed(lib)
    x = cache.item_json(a)
    assert set(x) == KEYS and x in cache.items_json() and len(cache.items_json()) == 2
    assert (x["id"], x["n"], x["dir"], x["k"], x["sc"], x["s"], x["lv"], x["th"], x["pv"]) == \
        (a, "IMG_1.JPG", "originals/2024/01", "p", 0, len(b"photo a"), 0, 0, 0)
    assert (x["f"], x["hi"], x["al"], x["me"], x["tr"], x["ta"], x["r"], x["left"], x["g"], x["b"]) == \
        (0, 0, [], [], 0, None, None, None, None, 0)
    assert cache.item_json("0" * 64) is None


def test_marks_and_trash_come_from_the_log(lib):
    a, b = icloud_import(lib)
    now = events.utcnow()
    events.append(lib, [ev("favorite", a, copy=lib.copy_id), ev("hide", a, copy=lib.copy_id),
                        ev("album.add", a, copy=lib.copy_id, kind="album", album="Trip"),
                        ev("album.add", a, copy=lib.copy_id, kind="memory", album="Summer"),
                        ev("trash", b, ts=now, copy=lib.copy_id, reason="Deleted in Photos")])
    cache = refreshed(lib)
    x, y = cache.item_json(a), cache.item_json(b)
    assert (x["f"], x["hi"], x["al"], x["me"], x["tr"], x["left"]) == (1, 1, ["Trip"], ["Summer"], 0, None)
    assert (y["tr"], y["ta"], y["r"], y["left"]) == (1, now, "Deleted in Photos", 30)
    assert (y["dir"], y["n"]) == (f"trash/{b}", "IMG_2.JPG")


@pytest.mark.parametrize("days, left", [(0, 30), (29.5, 1), (31, 0)])
def test_whole_days_left_in_trash(lib, days, left):
    x = add_item(lib, b"photo")
    events.append(lib, [ev("trash", x, ts=ts_str(datetime.now(UTC) - timedelta(days=days)), copy=lib.copy_id)])
    assert refreshed(lib).item_json(x)["left"] == left


def test_live_photo_shows_its_clip(lib):
    item = sha(b"photo")
    e = imp(item, name="IMG_3.HEIC", live="IMG_3.MOV", live_sha=sha(b"clip"), import_id=IMPORT, copy=lib.copy_id)
    e["files"][0]["size"], e["files"][1]["size"] = 10, 5
    events.append(lib, [e])
    add_source(lib, IMPORT, {"part1/Photos/IMG_3.HEIC": b"photo", "part1/Photos/IMG_3.MOV": b"clip"},
               exif=[{"SourceFile": "raw/part1/Photos/IMG_3.HEIC", "CompressorID": None},
                     {"SourceFile": "raw/part1/Photos/IMG_3.MOV", "CompressorID": "hvc1"}])
    x = refreshed(lib).item_json(item)
    assert (x["lv"], x["lc"], x["s"], x["k"], x["c"]) == (1, "hvc1", 15, "p", None)


def test_provider_follows_the_adapter(lib, mexico):
    takeout, bare = "2026-01-05-takeout-000001", "2026-01-05-icloud-000002"
    x = add_item(lib, b"x", name="IMG_1.JPG", import_id=takeout)
    y = add_item(lib, b"y", name="IMG_2.JPG", import_id=bare)
    z = add_item(lib, b"z", name="20240104_113456.jpg")
    date = "Thursday January 4,2024 5:34 PM GMT"
    add_source(lib, takeout, {"part1/Photos/IMG_1.JPG": b"x"}, dates={"IMG_1.JPG": date}, adapter="takeout")
    add_source(lib, bare, {"part1/Photos/IMG_2.JPG": b"y"}, dates={"IMG_2.JPG": date}, adapter=None)
    cache = refreshed(lib)
    assert (cache.item_json(x)["ds"], cache.item_json(x)["o"]) == ("mtime", "part1/Photos/IMG_1.JPG")
    assert cache.item_json(y)["ds"] == "provider"  # adapter read from the import id
    assert (cache.item_json(z)["ds"], cache.item_json(z)["o"]) == ("filename", None)


def test_refresh_without_pillow_skips_rendering(lib, monkeypatch):
    icloud_import(lib)
    monkeypatch.setattr(media, "available", lambda: (False, ["pillow", "numpy"]))
    for module in ("PIL", "numpy"):
        monkeypatch.setitem(sys.modules, module, None)  # importing them now fails
    cache = Cache(lib)
    assert cache.refresh() == {"items": 2, "rendered": 0, "errors": 0, "skipped_render": 2, "source_errors": 0}
    assert [i["th"] for i in cache.items_json()] == [0, 0]
    assert not (lib.cache / "thumbs").exists()
    with closing(sqlite3.connect(lib.cache / "index.sqlite")) as db:
        [(at,)] = db.execute("SELECT value FROM meta WHERE key = 'refreshed_at'").fetchall()
    assert at.endswith("Z")


@needs_media
def test_second_refresh_renders_only_what_is_missing(lib):
    x = add_item(lib, jpeg_bytes((200, 80, 40)), name="A.JPG")
    cache = Cache(lib)
    assert cache.refresh(workers=1) == {"items": 1, "rendered": 1, "errors": 0, "skipped_render": 0,
                                        "source_errors": 0}
    assert cache.thumb_path(x).is_file() and cache.item_json(x)["th"] == 1
    add_item(lib, jpeg_bytes((10, 90, 200)), name="B.JPG")
    assert cache.refresh(workers=1) == {"items": 2, "rendered": 1, "errors": 0, "skipped_render": 0,
                                        "source_errors": 0}
    cache.thumb_path(x).unlink()
    assert cache.item_json(x)["th"] == 0  # no broken image before the next render
    assert cache.refresh(workers=1)["rendered"] == 1
    assert cache.refresh(workers=1)["rendered"] == 0
    assert (cache.item_json(x)["w"], cache.item_json(x)["sh"] is not None) == (8, True)


@needs_media
def test_refresh_says_how_far_rendering_got_every_few_seconds(lib):
    for n in range(3):
        add_item(lib, jpeg_bytes((n * 80, 0, 0)), name=f"{n}.JPG")
    said: list[str] = []
    Cache(lib).refresh(workers=1, progress=said.append)
    assert said == ["rendering thumbnails: 3/3"]  # three quick files: only the last line


@needs_media
def test_unreadable_file_is_an_error_not_a_failure(lib):
    x = add_item(lib, b"not a jpeg", name="BAD.JPG")
    cache = Cache(lib)
    assert cache.refresh(workers=1) == {"items": 1, "rendered": 0, "errors": 1, "skipped_render": 0,
                                        "source_errors": 0}
    assert cache.item_json(x)["th"] == 0


def test_render_false_counts_what_it_skipped(lib):
    icloud_import(lib)
    assert Cache(lib).refresh(render=False) == {"items": 2, "rendered": 0, "errors": 0, "skipped_render": 2,
                                                "source_errors": 0}


def test_similar_photos_are_grouped_but_hidden_ones_are_not(lib):
    pytest.importorskip("numpy")
    x, y, z = (add_item(lib, bytes([n]), name=f"{n}.JPG") for n in range(3))
    cache = refreshed(lib)
    seed_media(lib, {x: {"w": 100, "h": 100, "dhash": f"{0:016x}"},
                     y: {"w": 200, "h": 200, "dhash": f"{0b1111:016x}"},
                     z: {"w": 300, "h": 300, "dhash": f"{0b11:016x}"}})
    mark(lib, "hide", z)
    cache.refresh(render=False)
    got = {i: (cache.item_json(i)["g"], cache.item_json(i)["b"]) for i in (x, y, z)}
    assert got == {x: (min(x, y), 0), y: (min(x, y), 1), z: (None, 0)}


def test_measured_size_wins_over_exif(lib):
    a, _ = icloud_import(lib)
    cache = refreshed(lib)
    seed_media(lib, {a: {"w": 3024, "h": 4032, "sharp": 2.5, "thumb": 1, "preview": 1}})  # rendered upright
    x = cache.item_json(a)
    assert (x["w"], x["h"], x["sh"], x["th"], x["pv"]) == (3024, 4032, 2.5, 0, 0)  # no files yet
    for path in (cache.thumb_path(a), cache.preview_path(a)):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"jpeg")
    assert (cache.item_json(a)["th"], cache.item_json(a)["pv"]) == (1, 1)


def test_purged_item_leaves_the_index_with_its_thumbnails(lib):
    x = add_item(lib, b"photo")
    cache = refreshed(lib)
    seed_media(lib, {x: {"thumb": 1, "preview": 1}})
    for path in (cache.thumb_path(x), cache.preview_path(x)):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"jpeg")
    mark(lib, "trash", x)
    mark(lib, "purge", x, ts=T2)
    assert cache.refresh(render=False)["items"] == 0
    assert cache.items_json() == []
    assert not cache.thumb_path(x).exists() and not cache.preview_path(x).exists()
    with closing(sqlite3.connect(lib.cache / "index.sqlite")) as db:
        assert db.execute("SELECT count(*) FROM media").fetchone() == (0,)


def test_refresh_items_rewrites_only_the_named_rows(lib):
    a, b, c = (add_item(lib, data, name=name) for data, name in ((b"a", "A.JPG"), (b"b", "B.JPG"), (b"c", "C.JPG")))
    cache = refreshed(lib)
    with closing(sqlite3.connect(lib.cache / "index.sqlite")) as db, db:
        db.execute("UPDATE items SET g = 'g1', b = 1 WHERE id = ?", (a,))
    for item in (a, b):
        mark(lib, "favorite", item)
    mark(lib, "trash", c)
    mark(lib, "purge", c, ts=T2)
    cache.refresh_items([a, c])
    x, y = cache.item_json(a), cache.item_json(b)
    assert (x["f"], x["g"], x["b"]) == (1, "g1", 1)  # rewritten from the log, similarity group kept
    assert y["f"] == 0  # not named: as the last refresh left it
    assert cache.item_json(c) is None
    assert [r["id"] for r in cache.items_json([b, "0" * 64])] == [b]


def test_refresh_items_never_deletes_through_a_symlinked_folder(lib, tmp_path):
    x = add_item(lib, b"photo")
    cache = refreshed(lib)
    outside = tmp_path / "outside"
    (outside / x[:2]).mkdir(parents=True)
    (outside / x[:2] / f"{x}.jpg").write_bytes(b"keep")
    (lib.cache / "thumbs").symlink_to(outside)
    mark(lib, "trash", x)
    mark(lib, "purge", x, ts=T2)
    seed_media(lib, {x: {"thumb": 1}})
    with pytest.raises(LibraryError, match="symlink"):
        cache.refresh_items([x])
    assert (outside / x[:2] / f"{x}.jpg").read_bytes() == b"keep"


def test_an_import_is_read_again_only_when_its_files_change(lib, monkeypatch):
    a, _ = icloud_import(lib)
    cache = refreshed(lib)
    reads = []
    real = cache_module._import_sources
    monkeypatch.setattr(cache_module, "_import_sources", lambda folder: reads.append(folder.name) or real(folder))
    cache.refresh_items([a])
    cache.refresh(render=False)
    assert reads == [] and cache.item_json(a)["cam"] == "Apple iPhone 15"
    put(lib, f"sources/{IMPORT}/exif.json", b"[]")
    cache.refresh_items([a])
    assert reads == [IMPORT] and cache.item_json(a)["cam"] is None


def test_thumb_and_preview_paths(lib):
    cache, x = Cache(lib), "ab" + "0" * 62
    assert cache.thumb_path(x) == lib.cache / "thumbs" / "ab" / f"{x}.jpg"
    assert cache.preview_path(x) == lib.cache / "previews" / "ab" / f"{x}.jpg"
    with pytest.raises(ValueError):
        cache.thumb_path("../" * 3 + "x")


@pytest.mark.parametrize("name", ["index.sqlite", "index.sqlite-wal", "index.sqlite-journal"])
def test_symlinked_index_is_refused(lib, tmp_path, name):
    target = tmp_path / "target"
    target.write_bytes(b"keep")
    (lib.cache / name).symlink_to(target)
    with pytest.raises(LibraryError, match="symlink"):
        Cache(lib).refresh(render=False)
    assert target.read_bytes() == b"keep"


def test_symlinked_hash_cache_is_refused(lib, tmp_path):
    (lib.cache / "files.sqlite").symlink_to(tmp_path / "target")
    with pytest.raises(LibraryError, match="symlink"):
        HashCache(lib)
    assert not (tmp_path / "target").exists()


def test_thumbnails_are_never_written_through_a_symlink(lib, tmp_path):
    x = add_item(lib, b"photo")
    outside = tmp_path / "outside"
    outside.mkdir()
    (lib.cache / "thumbs").mkdir()
    (lib.cache / "thumbs" / x[:2]).symlink_to(outside)
    with pytest.raises(LibraryError, match="symlink"):
        Cache(lib).refresh()
    (lib.cache / "thumbs" / x[:2]).unlink()
    (lib.cache / "thumbs").rmdir()
    (lib.cache / "previews").symlink_to(outside)
    with pytest.raises(LibraryError, match="symlink"):
        Cache(lib).refresh(render=False)
    assert list(outside.iterdir()) == []


def test_cli_cache_refresh(lib, capsys):
    icloud_import(lib)
    assert main(["cache", "refresh", str(lib.root), "--no-render", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"items": 2, "rendered": 0, "errors": 0, "skipped_render": 2,
                                                   "source_errors": 0}
    assert main(["cache", "refresh", str(lib.root), "--no-render"]) == 0
    assert "items: 2" in capsys.readouterr().out
    assert main(["cache", "refresh", str(lib.root / "nowhere")]) == 1


def test_unchanged_input_reuses_the_groups(lib, monkeypatch):
    pytest.importorskip("numpy")
    x, y = (add_item(lib, bytes([n]), name=f"{n}.JPG") for n in range(2))
    cache = refreshed(lib)
    seed_media(lib, {x: {"dhash": f"{0:016x}"}, y: {"dhash": f"{0b11:016x}"}})
    calls, real = [], media.similar_groups
    monkeypatch.setattr(media, "similar_groups", lambda rows: calls.append(len(rows)) or real(rows))
    cache.refresh(render=False)
    cache.refresh(render=False)
    assert calls == [2] and [cache.item_json(i)["g"] for i in (x, y)] == [min(x, y)] * 2
    mark(lib, "favorite", x)
    cache.refresh(render=False)
    assert calls == [2, 2]


def test_truncated_photo_details_row_does_not_stop_refresh(lib):
    a, b = icloud_import(lib)
    put(lib, f"sources/{IMPORT}/part1/Photos/Photo Details.csv",
        (DETAILS_HEADER + 'IMG_1.JPG,x,no,no,no,"Monday January 1,2024 9:00 AM GMT",0\nIMG_2.JPG,x\n').encode())
    cache = Cache(lib)
    assert cache.refresh(render=False)["source_errors"] == 0
    assert (cache.item_json(a)["ds"], cache.item_json(b)["ds"]) == ("exif", "mtime")


def test_unreadable_sources_are_counted_not_fatal(lib, monkeypatch):
    a, b = icloud_import(lib)
    put(lib, "sources/2026-01-06-icloud-ffffff/manifest.json", b'{"files": [')
    put(lib, f"sources/{IMPORT}/exif.json", b"[")

    def damaged(folder):
        raise RuntimeError("damaged export")

    monkeypatch.setitem(cache_module.PROVIDERS, "icloud", damaged)
    cache = Cache(lib)
    assert cache.refresh(render=False)["source_errors"] == 3  # manifest, exif.json, provider
    assert [(i["ds"], i["o"]) for i in cache.items_json()] == [("mtime", "part1/Photos/IMG_1.JPG"),
                                                              ("mtime", "part1/Photos/IMG_2.JPG")]


def test_odd_metadata_falls_back_to_what_the_log_knows(lib, monkeypatch):
    x = add_item(lib, b"x", name="IMG_5.JPG", import_id=IMPORT)
    add_source(lib, IMPORT, {"part1/Photos/IMG_5.JPG": b"x"},
               exif=[{"SourceFile": "part1/Photos/IMG_5.JPG", "Make": "Apple", "Model": 5}])
    assert refreshed(lib).item_json(x)["cam"] == "Apple 5"  # exiftool writes numeric-looking text as a number
    real = cache_module.derive
    monkeypatch.setattr(cache_module, "derive", lambda exif, *rest: real(exif, *rest) if not exif else 1 / 0)
    cache = Cache(lib)
    assert cache.refresh(render=False)["source_errors"] == 1
    assert (cache.item_json(x)["ds"], cache.item_json(x)["o"], cache.item_json(x)["cam"]) == ("mtime", None, None)


def test_unreadable_trash_time_shows_no_days_left(lib):
    x, y = add_item(lib, b"x", name="X.JPG"), add_item(lib, b"y", name="Y.JPG")
    events.append(lib, [ev("trash", x, ts="2026-01-02T00:00:00", copy=lib.copy_id),
                        ev("trash", y, ts="2026-01-02 garbage", copy=lib.copy_id)])
    assert [(i["tr"], i["left"]) for i in refreshed(lib).items_json()] == [(1, None), (1, None)]


@needs_media
def test_a_renderer_crash_costs_only_its_own_file(lib, monkeypatch):
    good = [add_item(lib, jpeg_bytes((n * 60, 80, 40)), name=f"G{n}.JPG") for n in range(3)]
    bad = add_item(lib, jpeg_bytes((1, 2, 3)), name="CRASH.JPG")
    monkeypatch.setattr(media, "render", render_or_die)
    cache = Cache(lib)
    said: list[str] = []
    assert cache.refresh(workers=2, progress=said.append) == {"items": 4, "rendered": 3, "errors": 1,
                                                              "skipped_render": 0, "source_errors": 0}
    assert said[-1].startswith("rendering thumbnails one at a time: ")
    assert [cache.item_json(i)["th"] for i in good] == [1, 1, 1] and cache.item_json(bad)["th"] == 0
    with closing(sqlite3.connect(lib.cache / "index.sqlite")) as db:
        [(error,)] = db.execute("SELECT error FROM media WHERE id = ?", (bad,)).fetchall()
    assert error.startswith("BrokenProcessPool")


@pytest.mark.parametrize("db, argv", [("index.sqlite", ["cache", "refresh", "A"]), ("files.sqlite", ["verify", "A"]),
                                      ("files.sqlite", ["sync", "A", "B"]),
                                      ("files.sqlite", ["repair", "A", "--from", "B"])])
def test_damaged_cache_database_is_a_one_line_error(a, b, capsys, db, argv):
    add_item(a, b"photo")
    (a.cache / db).write_bytes(b"not a database" * 100)
    assert main([{"A": str(a.root), "B": str(b.root)}.get(arg, arg) for arg in argv]) == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "cache/" in err and "deleted" in err

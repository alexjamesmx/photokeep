import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from photokeep import events, fsops, hashcache
from photokeep import sync as sync_module
from photokeep.cache import Cache
from photokeep.cli import main
from photokeep.events import LogCorrupt, append_lines, read_all, read_log, ts_str
from photokeep.fsops import CopyError
from photokeep.library import Library, LibraryError, init_library, locked, open_library
from photokeep.state import fold
from photokeep.sync import exchange_logs, plan, run
from tests.helpers import (T0, T1, T2, add_item, add_source, fail_after, files_of, imp, mark, no_part_files, put, sha,
                           snapshot)

NOW = datetime(2026, 1, 2, tzinfo=UTC)
TRASHED = datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)  # T1, when mark() trashes
CLEAN = {"moves": 0, "deletes": 0, "missing": 0, "untracked": 0, "conflicts": 0}
NO_APP = {"direction": None, "a": None, "b": None}


def logs_of(lib: Library) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in lib.log_dir.iterdir()}


def app_files(lib: Library) -> dict[str, bytes]:
    return {p.relative_to(lib.app).as_posix(): p.read_bytes() for p in lib.app.rglob("*") if p.is_file()}


def read_log_line(lib: Library, n: int) -> bytes:
    return lib.log_path().read_bytes().splitlines(keepends=True)[n]


def corrupt_same_length(a: Library, b: Library, file: str) -> None:
    """Give b's copy of `file` the bytes of a's with one id digit changed: same length, still valid JSON."""
    data = (a.log_dir / file).read_bytes()
    i = data.rindex(b'"id":"') + len(b'"id":"')
    digit = b"1" if data[i:i + 1] == b"0" else b"0"
    (b.log_dir / file).write_bytes(data[:i] + digit + data[i + 1:])


def test_new_items_flow_both_ways(a, b):
    x = add_item(a, b"from a", name="A.JPG")
    y = add_item(b, b"from b", name="B.JPG", folder="2024/02")
    run(a, b, NOW)
    assert files_of(a) == files_of(b) == {"originals/2024/01/A.JPG": b"from a", "originals/2024/02/B.JPG": b"from b"}
    assert logs_of(a) == logs_of(b)
    assert {i.id for i in fold(read_all(b)).live()} == {x, y}


def test_sync_refreshes_both_caches(a, b):
    x = add_item(a, b"from a", name="A.JPG")
    y = add_item(b, b"from b", name="B.JPG")
    run(a, b, NOW)
    for lib in (a, b):
        assert sorted(i["id"] for i in Cache(lib).items_json()) == sorted([x, y])


def test_trash_on_one_side_moves_file_on_the_other(a, b):
    x = add_item(a, b"photo", name="A.JPG")
    run(a, b, NOW)
    mark(b, "trash", x)
    run(a, b, NOW)
    assert files_of(a) == files_of(b) == {f"trash/{x}/A.JPG": b"photo"}


def test_trashed_item_is_copied_into_the_other_trash(a, b):
    x = add_item(a, b"photo", name="A.JPG")
    mark(a, "trash", x)
    run(a, b, NOW)
    assert files_of(a) == files_of(b) == {f"trash/{x}/A.JPG": b"photo"}


def test_separate_edits_both_survive(a, b):
    x = add_item(a, b"photo")
    run(a, b, NOW)
    mark(a, "favorite", x, ts=T1)
    mark(b, "hide", x, ts=T2)
    run(a, b, NOW)
    for lib in (a, b):
        item = fold(read_all(lib)).items[x]
        assert item.favorite and item.hidden


def test_purge_after_30_days_deletes_everywhere(a, b):
    x = add_item(a, b"photo", name="A.JPG")
    run(a, b, NOW)
    mark(b, "trash", x)
    assert run(a, b, TRASHED + timedelta(days=29)).new_purges == []
    assert files_of(a) == files_of(b) == {f"trash/{x}/A.JPG": b"photo"}
    later = TRASHED + timedelta(days=31)
    assert run(a, b, later).new_purges == [(x, T1)]
    assert files_of(a) == files_of(b) == {}
    [purge] = [e for e in read_log(a.log_path()) if e["op"] == "purge"]
    assert purge["item"] == x and purge["ts"] == ts_str(later)


def test_purge_events_reach_b_log(a, b):
    x = add_item(a, b"photo")
    mark(a, "trash", x)
    run(a, b, TRASHED + timedelta(days=31))
    assert [e["item"] for e in read_log(b.log_path(a.copy_id)) if e["op"] == "purge"] == [x]
    assert fold(read_all(b)).items[x].status == "purged"
    assert logs_of(a) == logs_of(b)


def test_rerun_after_interrupted_copy_completes(a, b, monkeypatch):
    for n in range(3):
        add_item(a, f"photo {n}".encode(), name=f"P{n}.JPG")
    monkeypatch.setattr(fsops, "copy_verified", fail_after(1))
    with pytest.raises(CopyError):
        run(a, b, NOW)
    assert not no_part_files(b)
    monkeypatch.undo()
    run(a, b, NOW)
    assert files_of(a) == files_of(b) and len(files_of(b)) == 3 and no_part_files(a, b)


def test_diverged_logs_stop_and_change_nothing(a, b):
    add_item(a, b"photo")
    run(a, b, NOW)
    add_item(b, b"only on b", name="B.JPG")
    corrupt_same_length(a, b, file=f"{a.copy_id}.jsonl")
    before = snapshot(a, b)
    with pytest.raises(LogCorrupt, match=a.copy_id):
        run(a, b, NOW)
    assert snapshot(a, b) == before


def test_invalid_line_is_never_copied(a, b):
    with open(a.log_path(), "ab") as f:
        f.write(b'{"v":1,"op":"favorite"}\n')
    before = snapshot(a, b)
    with pytest.raises(LogCorrupt, match=a.copy_id):
        run(a, b, NOW)
    assert snapshot(a, b) == before


def test_known_op_missing_its_fields_is_never_copied(a, b):
    bad = imp(sha(b"x"), copy=a.copy_id)
    del bad["files"]
    events.append(a, [bad])
    before = snapshot(a, b)
    with pytest.raises(LogCorrupt, match=rf"{a.copy_id}\.jsonl: line 2 is not a valid import event"):
        run(a, b, NOW)
    assert snapshot(a, b) == before


def test_log_that_is_a_symlink_is_refused(a, b, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"keep")
    (b.log_dir / f"{a.copy_id}.jsonl").symlink_to(outside)
    with pytest.raises(LogCorrupt, match="not a regular file"):
        run(a, b, NOW)
    assert outside.read_bytes() == b"keep"


def test_exchange_cuts_a_torn_tail_before_appending(a, b):
    name = a.log_path().name
    exchange_logs(a, b, dry_run=False)
    add_item(a, b"photo")
    with open(b.log_dir / name, "ab") as f:
        f.write(b'{"v":1,"id":"to')
    before = snapshot(a, b)
    [(file, direction, size)] = exchange_logs(a, b, dry_run=True)
    assert snapshot(a, b) == before
    assert (file, direction, size) == (name, "a->b", len(read_log_line(a, -1)))
    exchange_logs(a, b, dry_run=False)
    assert (b.log_dir / name).read_bytes() == a.log_path().read_bytes()


def test_exchange_never_copies_a_torn_tail(a, b):
    complete = a.log_path().read_bytes()
    with open(a.log_path(), "ab") as f:
        f.write(b'{"v":1,"id":"to')
    exchange_logs(a, b, dry_run=False)
    assert (b.log_dir / a.log_path().name).read_bytes() == complete


def test_log_copy_refuses_when_the_log_changed_since_it_was_compared(a, b):
    exchange_logs(a, b, dry_run=False)
    log = b.log_dir / a.log_path().name
    before = log.read_bytes()
    with pytest.raises(LogCorrupt, match="changed"):
        append_lines(log, read_log_line(a, -1), at=len(before) - 1)
    assert log.read_bytes() == before


def test_different_libraries_refuse(tmp_path):
    a, b = init_library(tmp_path / "A", "x"), init_library(tmp_path / "B", "y")
    before = snapshot(a, b)
    for step in (plan, run):
        with pytest.raises(LibraryError, match="different libraries"):
            step(a, b, NOW)
    assert snapshot(a, b) == before


def test_a_copy_cannot_sync_with_itself(a):
    with pytest.raises(LibraryError, match="same copy"):
        run(a, open_library(a.root), NOW)


def test_dry_run_plan_writes_nothing(a, b):
    add_item(a, b"from a", name="A.JPG")
    add_item(b, b"from b", name="B.JPG")
    old = add_item(a, b"old", name="O.JPG")
    mark(a, "trash", old)
    put(b, "originals/2024/01/A.JPG.photokeep-part", b"half")  # left by an interrupted run
    now = TRASHED + timedelta(days=31)
    before = snapshot(a, b)
    s = plan(a, b, now).summary()
    assert snapshot(a, b) == before
    assert s["new_purges"] == [{"item": old, "trashed_at": T1}]
    assert (s["copy_a_to_b"], s["copy_b_to_a"], s["missing_both"], s["conflicts"], s["bad_sources"]) == (1, 1, 0, 0, [])
    assert [(f, d) for f, d, _ in s["logs"]] == sorted([(f"{a.copy_id}.jsonl", "a->b"), (f"{b.copy_id}.jsonl", "b->a")])
    assert s["layout_a"] == CLEAN | {"deletes": 1, "missing": 1} and s["layout_b"] == CLEAN | {"missing": 1}
    assert s["app"] == NO_APP and s["pending"] is None
    assert run(a, b, now).summary() == s | {"pending": {"a": CLEAN, "b": CLEAN}}


def test_same_name_imported_on_both_sides_ends_suffixed_on_both(a, b):
    add_item(a, b"first", name="N.JPG", ts=T0)
    y = add_item(b, b"second", name="N.JPG", ts=T1)
    run(a, b, NOW)
    want = {"originals/2024/01/N.JPG": b"first", f"originals/2024/01/N~{y[:8]}.JPG": b"second"}
    assert files_of(a) == files_of(b) == want


def test_different_bytes_at_expected_path_are_kept_and_counted(a, b):
    add_item(a, b"photo", name="A.JPG")
    put(b, "originals/2024/01/A.JPG", b"something else")
    s = run(a, b, NOW).summary()
    assert s["conflicts"] == 1 and s["copy_a_to_b"] == 0 and s["pending"]["b"]["conflicts"] == 1
    assert (b.root / "originals/2024/01/A.JPG").read_bytes() == b"something else"


def test_file_missing_on_both_sides_is_reported(a, b):
    x = add_item(a, b"lost")
    (a.originals / "2024/01/IMG_1.JPG").unlink()
    result = run(a, b, NOW)
    assert result.missing_both == [(x, "originals/2024/01/IMG_1.JPG")] and result.summary()["missing_both"] == 1
    assert main(["sync", str(a.root), str(b.root), "--yes"]) == 2


def test_rotted_source_is_skipped_and_listed_while_the_rest_is_copied(a, b):
    rotten = add_item(a, b"rots later", name="A.JPG")
    add_item(a, b"stays good", name="B.JPG")
    plan(a, b, NOW)  # a's hash cache now vouches for A.JPG
    path = a.originals / "2024/01/A.JPG"
    st = path.stat()
    path.write_bytes(b"R" + b"rots later"[1:])
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    s = run(a, b, NOW).summary()
    assert s["bad_sources"] == [{"side": "a", "path": "originals/2024/01/A.JPG", "sha": rotten}]
    assert files_of(b) == {"originals/2024/01/B.JPG": b"stays good"} and no_part_files(b)
    assert main(["sync", str(a.root), str(b.root), "--yes"]) == 2


def test_app_is_never_replaced_without_the_flag(a, b):
    put(a, "app/photokeep/__init__.py", b'__version__ = "0.10.0"\n')
    put(b, "app/photokeep/__init__.py", b'__version__ = "0.9.1"\n')
    before = app_files(b)
    assert run(a, b, NOW).summary()["app"] == {"direction": "a->b", "a": "0.10.0", "b": "0.9.1"}
    assert app_files(b) == before


def test_newer_app_copied_to_older_side_with_the_flag(a, b):
    put(a, "app/photokeep/__init__.py", b'__version__ = "0.10.0"\n')
    put(a, "app/photokeep/new.py", b"new")
    put(b, "app/photokeep/__init__.py", b'__version__ = "0.9.1"\n')
    put(b, "app/photokeep/stale.py", b"old")
    assert plan(a, b, NOW).app.direction == "a->b"
    assert run(a, b, NOW, update_app=True).app.direction == "a->b"
    assert app_files(b) == app_files(a) == {"photokeep/__init__.py": b'__version__ = "0.10.0"\n',
                                            "photokeep/new.py": b"new"}
    assert run(a, b, NOW, update_app=True).summary()["app"] == {"direction": None, "a": "0.10.0", "b": "0.10.0"}
    put(b, "app/photokeep/__init__.py", b'__version__ = "0.11"\n')
    assert run(a, b, NOW, update_app=True).app.direction == "b->a" and app_files(a) == app_files(b)
    assert [p for p in (*a.cache.iterdir(), *b.cache.iterdir()) if p.is_dir()] == []


def test_app_containing_a_symlink_is_refused_before_anything_changes(a, b, tmp_path):
    put(a, "app/photokeep/__init__.py", b'__version__ = "0.2.0"\n')
    (tmp_path / "anywhere.py").write_bytes(b"code from elsewhere")
    (a.app / "photokeep" / "evil.py").symlink_to(tmp_path / "anywhere.py")
    put(b, "app/photokeep/__init__.py", b'__version__ = "0.1.0"\n')
    add_item(a, b"photo")
    before = snapshot(a, b)
    with pytest.raises(LibraryError, match="symlink"):
        run(a, b, NOW, update_app=True)
    assert snapshot(a, b) == before


def test_app_update_skipped_when_absent_or_equal(a, b):
    put(a, "app/photokeep/__init__.py", b'__version__ = "0.2.0"\n')
    assert run(a, b, NOW, update_app=True).app.direction is None and app_files(b) == {}
    put(b, "app/photokeep/__init__.py", b'__version__ = "0.2.0"\n')
    put(b, "app/photokeep/x.py", b"x")
    assert run(a, b, NOW, update_app=True).app.direction is None and "photokeep/x.py" not in app_files(a)


def test_app_swap_cut_off_between_renames_is_finished_by_the_next_run(a, b):
    put(a, "app/photokeep/__init__.py", b'__version__ = "0.2.0"\n')
    put(b, "app/photokeep/__init__.py", b'__version__ = "0.1.0"\n')
    (b.cache / "app-update-crashed").mkdir()
    b.app.rename(b.cache / "app-update-crashed" / "old")
    open_library(b.root)  # a missing app/ must not lock the owner out of the repair
    assert run(a, b, NOW, update_app=True).app.direction == "a->b" and app_files(b) == app_files(a)
    assert not (b.cache / "app-update-crashed").exists()


def test_copy_never_writes_through_a_symlinked_folder(a, b, tmp_path):
    add_item(a, b"photo")
    outside = tmp_path / "outside"
    outside.mkdir()
    (b.originals / "2024").symlink_to(outside)
    result = run(a, b, NOW)
    assert list(outside.iterdir()) == [] and result.pending[1].summary()["missing"] == 1


def test_copied_files_are_not_read_again_to_hash_them(a, b, monkeypatch):
    add_item(a, b"photo")
    reads = []
    real = hashcache.file_sha256
    monkeypatch.setattr(hashcache, "file_sha256", lambda path: reads.append(path) or real(path))
    run(a, b, NOW)
    assert files_of(b) and not [p for p in reads if p.is_relative_to(b.root)]


def test_lock_busy_raises(a, b):
    add_item(a, b"photo")
    before = snapshot(a, b)
    with locked(b), pytest.raises(LibraryError, match="busy"):
        run(a, b, NOW)
    assert snapshot(a, b) == before
    run(a, b, NOW)
    assert files_of(b) == files_of(a)


def test_cli_sync_dry_run_then_yes(a, b, capsys):
    add_item(a, b"photo")
    before = snapshot(a, b)
    assert main(["sync", str(a.root), str(b.root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["copy_a_to_b"] == 1
    assert main(["sync", str(a.root), str(b.root)]) == 0
    assert "files: 1 a->b, 0 b->a" in capsys.readouterr().out
    assert snapshot(a, b) == before
    assert main(["sync", str(a.root), str(b.root), "--yes"]) == 0
    assert "pending b: 0 moves" in capsys.readouterr().out
    assert files_of(b) == files_of(a)


def test_cli_sync_exits_2_on_conflict(a, b, capsys):
    add_item(a, b"photo", name="A.JPG")
    put(b, "originals/2024/01/A.JPG", b"other")
    assert main(["sync", str(a.root), str(b.root)]) == 2
    assert main(["sync", str(a.root), str(b.root), "--yes", "--json"]) == 2
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["conflicts"] == 1


def test_cli_lists_due_purges_and_updates_the_app_only_when_asked(a, b, capsys):
    old = add_item(a, b"old", name="O.JPG")
    mark(a, "trash", old)
    put(a, "app/photokeep/__init__.py", b'__version__ = "0.2.0"\n')
    put(b, "app/photokeep/__init__.py", b'__version__ = "0.1.0"\n')
    assert main(["sync", str(a.root), str(b.root)]) == 0
    out = capsys.readouterr().out
    assert f"purges: 1 ({old[:12]})" in out and "--update-app" in out
    assert main(["sync", str(a.root), str(b.root), "--yes"]) == 0
    assert app_files(b) == {"photokeep/__init__.py": b'__version__ = "0.1.0"\n'}
    assert main(["sync", str(a.root), str(b.root), "--yes", "--update-app", "--json"]) == 0
    assert app_files(b) == app_files(a)


def test_cli_sync_reports_errors(tmp_path, capsys):
    a, b = init_library(tmp_path / "A", "x"), init_library(tmp_path / "B", "y")
    assert main(["sync", str(a.root), str(b.root)]) == 1
    assert "different libraries" in capsys.readouterr().err


IMPORT = "2026-01-05-icloud-a1b2c3"
SOURCE_FILES = ("manifest.json", "exif.json", "part1/Photos/Photo Details.csv")


def sources_of(lib: Library) -> dict[str, bytes]:
    return {p.relative_to(lib.sources).as_posix(): p.read_bytes() for p in lib.sources.rglob("*") if p.is_file()}


def test_import_dir_only_on_a_is_copied_and_b_shows_the_provider_date(a, b, mexico):
    x = add_item(a, b"photo", name="IMG_1.JPG", import_id=IMPORT)
    add_source(a, IMPORT, {"part1/Photos/IMG_1.JPG": b"photo"}, dates={"IMG_1.JPG": "Thursday January 4,2024 5:34 PM GMT"})
    s = run(a, b, NOW).summary()
    assert s["sources"] == {"a_to_b": 3, "b_to_a": 0, "conflicts": []}
    assert sources_of(b) == sources_of(a) and set(sources_of(b)) == {f"{IMPORT}/{f}" for f in SOURCE_FILES}
    assert (Cache(b).item_json(x)["t"], Cache(b).item_json(x)["ds"]) == ("2024-01-04T11:34:00", "provider")
    assert no_part_files(a, b)


def test_file_only_in_bs_import_dir_is_copied_to_a(a, b):
    add_source(a, IMPORT, {})
    run(a, b, NOW)
    put(b, f"sources/{IMPORT}/part2/Photo Details.csv", b"late")
    s = run(a, b, NOW).summary()
    assert s["sources"] == {"a_to_b": 0, "b_to_a": 1, "conflicts": []}
    assert sources_of(a) == sources_of(b) and sources_of(a)[f"{IMPORT}/part2/Photo Details.csv"] == b"late"


def test_same_path_with_different_bytes_is_a_conflict_and_left_alone(a, b):
    put(a, f"sources/{IMPORT}/exif.json", b"[1]")
    put(b, f"sources/{IMPORT}/exif.json", b"[2]")
    before = sources_of(a), sources_of(b)
    assert plan(a, b, NOW).summary()["sources"] == {"a_to_b": 0, "b_to_a": 0, "conflicts": [f"{IMPORT}/exif.json"]}
    assert main(["sync", str(a.root), str(b.root), "--yes"]) == 2
    assert (sources_of(a), sources_of(b)) == before


def test_dry_run_writes_nothing_to_sources(a, b):
    add_source(a, IMPORT, {"part1/Photos/IMG_1.JPG": b"x"})
    put(b, f"sources/{IMPORT}/extra.txt", b"y")
    before = snapshot(a, b)
    s = plan(a, b, NOW).summary()
    assert (s["sources"]["a_to_b"], s["sources"]["b_to_a"]) == (2, 1)
    assert snapshot(a, b) == before


def test_symlink_in_sources_is_skipped_and_reported(a, b, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    add_source(a, IMPORT, {})
    (a.sources / IMPORT / "link.txt").symlink_to(outside)
    s = run(a, b, NOW).summary()
    assert s["sources"]["conflicts"] == [f"{IMPORT}/link.txt"]
    assert not os.path.lexists(b.sources / IMPORT / "link.txt")
    assert set(sources_of(b)) == {f"{IMPORT}/manifest.json", f"{IMPORT}/exif.json"}


def test_symlink_against_a_real_file_is_not_overwritten(a, b, tmp_path):
    put(a, f"sources/{IMPORT}/x.txt", b"real")
    (b.sources / IMPORT).mkdir()
    (b.sources / IMPORT / "x.txt").symlink_to(tmp_path / "elsewhere")
    s = run(a, b, NOW).summary()
    assert s["sources"] == {"a_to_b": 0, "b_to_a": 0, "conflicts": [f"{IMPORT}/x.txt"]}
    assert (b.sources / IMPORT / "x.txt").is_symlink()


def test_foreign_names_and_part_files_in_sources_are_not_copied(a, b):
    put(a, "sources/notes/readme.txt", b"mine")
    put(a, f"sources/{IMPORT}/exif.json.photokeep-part", b"half")
    put(a, f"sources/{IMPORT}/exif.json", b"[]")
    s = run(a, b, NOW).summary()
    assert s["sources"] == {"a_to_b": 1, "b_to_a": 0, "conflicts": ["notes"]}
    assert sources_of(b) == {f"{IMPORT}/exif.json": b"[]"}


def test_interrupted_sources_copy_resumes(a, b, monkeypatch):
    add_source(a, IMPORT, {"part1/Photos/IMG_1.JPG": b"x"})
    monkeypatch.setattr(fsops, "copy_verified", fail_after(1))
    with pytest.raises(CopyError):
        run(a, b, NOW)
    monkeypatch.undo()
    run(a, b, NOW)
    assert sources_of(b) == sources_of(a) and no_part_files(a, b)


def test_sources_conflict_prints_and_exits_2(a, b, capsys):
    put(a, f"sources/{IMPORT}/exif.json", b"[1]")
    put(b, f"sources/{IMPORT}/exif.json", b"[2]")
    assert main(["sync", str(a.root), str(b.root), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["sources"]["conflicts"] == [f"{IMPORT}/exif.json"]
    assert main(["sync", str(a.root), str(b.root)]) == 2
    assert f"sources: 0 a->b, 0 b->a, sources conflicts: 1 ({IMPORT}/exif.json)" in capsys.readouterr().out


def test_symlinked_subfolder_on_b_is_never_written_through(a, b, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    put(a, f"sources/{IMPORT}/sub/file.txt", b"real")
    (b.sources / IMPORT).mkdir()
    (b.sources / IMPORT / "sub").symlink_to(outside)
    s = run(a, b, NOW).summary()
    assert s["sources"]["conflicts"] == [f"{IMPORT}/sub", f"{IMPORT}/sub/file.txt"]
    assert list(outside.iterdir()) == []


def test_link_that_appears_after_planning_is_refused(a, b, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    put(a, f"sources/{IMPORT}/sub/file.txt", b"real")
    (b.sources / IMPORT).mkdir()
    real = sync_module._compare_sources

    def compare_then_link(x, y):
        found = real(x, y)
        (b.sources / IMPORT / "sub").symlink_to(outside)
        return found

    monkeypatch.setattr(sync_module, "_compare_sources", compare_then_link)
    assert run(a, b, NOW).summary()["sources"] == {"a_to_b": 0, "b_to_a": 0, "conflicts": [f"{IMPORT}/sub/file.txt"]}
    assert list(outside.iterdir()) == []


def test_file_against_folder_of_the_same_name_is_a_conflict(a, b, capsys):
    put(a, f"sources/{IMPORT}/part1", b"file")
    put(b, f"sources/{IMPORT}/part1/Photo Details.csv", b"in a folder")
    assert main(["sync", str(a.root), str(b.root), "--yes"]) == 2
    out = capsys.readouterr().out
    assert f"sources conflicts: 2 ({IMPORT}/part1, {IMPORT}/part1/Photo Details.csv)" in out
    assert sources_of(a) == {f"{IMPORT}/part1": b"file"}


def test_unwritable_source_name_is_reported_and_the_rest_synced(a, b, monkeypatch):
    add_item(a, b"photo", name="A.JPG")
    put(a, f"sources/{IMPORT}/odd.txt", b"x")
    put(a, f"sources/{IMPORT}/fine.txt", b"y")
    real = fsops.copy_verified

    def copy(src, dst, digest):
        if dst.name == "odd.txt":
            raise OSError("invalid name")
        real(src, dst, digest)

    monkeypatch.setattr(fsops, "copy_verified", copy)
    s = run(a, b, NOW).summary()
    assert s["sources"] == {"a_to_b": 1, "b_to_a": 0, "conflicts": [f"{IMPORT}/odd.txt: invalid name"]}
    assert files_of(b) == {"originals/2024/01/A.JPG": b"photo"} and sources_of(b) == {f"{IMPORT}/fine.txt": b"y"}


def test_source_with_wrong_bytes_goes_to_bad_sources(a, b, monkeypatch):
    put(a, f"sources/{IMPORT}/exif.json", b"[]")

    def copy(src, dst, digest):
        raise fsops.HashMismatch("rotted")

    monkeypatch.setattr(fsops, "copy_verified", copy)
    s = run(a, b, NOW).summary()
    assert s["bad_sources"] == [{"side": "a", "path": f"sources/{IMPORT}/exif.json", "sha": sha(b"[]")}]
    assert sources_of(b) == {}


def test_restore_cut_short_by_a_full_disk_is_never_purged(a, b, monkeypatch):
    x = add_item(a, b"precious", name="A.JPG")
    mark(a, "trash", x)
    run(a, b, TRASHED + timedelta(days=1))
    mark(b, "restore", x, ts=ts_str(TRASHED + timedelta(days=5)))
    real = os.write
    # the drive's restore line reaches the laptop only half at a time
    monkeypatch.setattr(os, "write", lambda fd, data: real(fd, bytes(data)[:len(data) // 2 or 1])
                        if b'"restore"' in bytes(data) else real(fd, data))
    s = run(a, b, TRASHED + timedelta(days=31)).summary()
    monkeypatch.undo()
    assert s["new_purges"] == [] and logs_of(a) == logs_of(b)
    assert files_of(a) == files_of(b) == {"originals/2024/01/A.JPG": b"precious"}


def test_logs_left_unequal_by_the_exchange_stop_the_sync(a, b, monkeypatch):
    x = add_item(a, b"precious", name="A.JPG")
    mark(a, "trash", x)
    monkeypatch.setattr(events, "append_lines", lambda *args, **kwargs: None)  # a write that was lost
    before = snapshot(a, b)
    with pytest.raises(LogCorrupt, match="still differ"):
        run(a, b, TRASHED + timedelta(days=31))
    assert snapshot(a, b) == before

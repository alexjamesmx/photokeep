import errno
import os

import pytest

from photokeep.fsops import CopyError, HashMismatch, LayoutPlan, apply_layout, copy_verified, plan_layout, scan
from photokeep.hashcache import HashCache
from photokeep.state import fold
from tests.helpers import T1, T2, T3, ev, imp, put, sha, state_with

CLEAN = {"moves": 0, "deletes": 0, "missing": 0, "untracked": 0, "conflicts": 0}


def test_trash_moves_file_into_trash_folder(lib, jpeg):
    put(lib, "originals/2024/01/A.JPG", jpeg)
    s = state_with(lib, jpeg, name="A.JPG", trashed=True)
    plan = plan_layout(lib, s, scan(lib))
    apply_layout(lib, plan)
    assert (lib.root / f"trash/{sha(jpeg)}/A.JPG").read_bytes() == jpeg
    assert lib.originals.is_dir() and not (lib.originals / "2024").exists()


def test_move_never_overwrites_a_file_that_appeared_after_planning(lib, jpeg):
    put(lib, "originals/2024/01/A.JPG", jpeg)
    plan = plan_layout(lib, state_with(lib, jpeg, name="A.JPG", trashed=True), scan(lib))
    put(lib, f"trash/{sha(jpeg)}/A.JPG", b"other")
    apply_layout(lib, plan)
    assert (lib.originals / "2024/01/A.JPG").read_bytes() == jpeg
    assert (lib.root / f"trash/{sha(jpeg)}/A.JPG").read_bytes() == b"other"


def test_restore_moves_file_back_and_removes_emptied_trash_folder(lib, jpeg):
    item = sha(jpeg)
    put(lib, f"trash/{item}/A.JPG", jpeg)
    s = fold([imp(item, name="A.JPG"), ev("trash", item, ts=T1), ev("restore", item, ts=T2)])
    apply_layout(lib, plan_layout(lib, s, scan(lib)))
    assert (lib.originals / "2024/01/A.JPG").read_bytes() == jpeg
    assert lib.trash.is_dir() and list(lib.trash.iterdir()) == []


def test_untracked_file_is_reported_not_deleted(lib):
    put(lib, "originals/2024/01/stray.jpg", b"x")
    plan = plan_layout(lib, fold([]), scan(lib))
    assert plan.untracked == ["originals/2024/01/stray.jpg"] and plan.deletes == []
    apply_layout(lib, plan)
    assert (lib.originals / "2024/01/stray.jpg").read_bytes() == b"x"


def test_tombstoned_file_is_untracked_and_never_deleted(lib):
    rel = "originals/2024/01/T.JPG"
    put(lib, rel, b"purged on a copy that never saw its import")
    s = fold([ev("purge", sha(b"purged on a copy that never saw its import"), name="T.JPG")])
    plan = plan_layout(lib, s, scan(lib))
    assert plan.untracked == [rel] and plan.deletes == []
    apply_layout(lib, plan)
    assert (lib.root / rel).exists()


def test_purged_file_is_deleted(lib, jpeg):
    rel = f"trash/{sha(jpeg)}/A.JPG"
    put(lib, rel, jpeg)
    plan = plan_layout(lib, state_with(lib, jpeg, name="A.JPG", trashed=True, purged=True), scan(lib))
    assert plan.deletes == [rel]
    apply_layout(lib, plan)
    assert not (lib.root / rel).exists() and list(lib.trash.iterdir()) == []


def test_delete_rereads_a_file_replaced_with_same_size_and_mtime(lib):
    purged, live = b"P" * 32, b"L" * 32
    rel = f"trash/{sha(purged)}/A.JPG"
    st = put(lib, rel, purged).stat()
    plan = plan_layout(lib, state_with(lib, purged, name="A.JPG", purged=True), scan(lib))
    os.utime(put(lib, rel, live), ns=(st.st_atime_ns, st.st_mtime_ns))
    apply_layout(lib, plan)
    assert (lib.root / rel).read_bytes() == live


def test_purged_file_is_deleted_before_a_kept_file_moves_into_its_name(lib):
    old, new = b"purged elsewhere", b"took the freed name"
    s = fold([imp(sha(old), name="N.JPG", ts=T1), ev("purge", sha(old), ts=T2), imp(sha(new), name="N.JPG", ts=T3)])
    put(lib, "originals/2024/01/N.JPG", old)
    put(lib, f"originals/2024/01/N~{sha(new)[:8]}.JPG", new)
    plan = plan_layout(lib, s, scan(lib))
    assert plan.deletes == ["originals/2024/01/N.JPG"] and plan.conflicts == []
    apply_layout(lib, plan)
    assert [p.name for p in (lib.originals / "2024/01").iterdir()] == ["N.JPG"]
    assert (lib.originals / "2024/01/N.JPG").read_bytes() == new


def test_file_shared_with_a_live_item_is_never_deleted(lib):
    clip, gone = b"clip", "f" * 64
    paired = imp(gone, name="P.JPG", live="P.MOV")
    paired["files"][1]["sha256"] = sha(clip)
    s = fold([paired, ev("purge", gone, ts=T1), imp(sha(clip), name="C.MOV", ts=T2)])
    put(lib, "originals/2024/01/C.MOV", clip)
    assert plan_layout(lib, s, scan(lib)).summary() == CLEAN


def test_conflicting_bytes_at_expected_path_are_kept(lib, jpeg):
    item = sha(jpeg)
    put(lib, "originals/2024/01/A.JPG", b"other")
    put(lib, f"trash/{item}/A.JPG", jpeg)
    s = fold([imp(item, name="A.JPG")])
    plan = plan_layout(lib, s, scan(lib))
    assert plan.conflicts == [("originals/2024/01/A.JPG", item, sha(b"other"))] and plan.moves == []
    apply_layout(lib, plan)
    assert (lib.originals / "2024/01/A.JPG").read_bytes() == b"other"
    assert (lib.root / f"trash/{item}/A.JPG").read_bytes() == jpeg


def test_name_freed_by_a_move_is_filled_in_the_same_run(lib):
    a, b = b"imported first elsewhere", b"imported here"
    s = fold([imp(sha(a), name="N.JPG", ts=T1), imp(sha(b), name="N.JPG", ts=T2)])
    put(lib, "originals/2024/01/N.JPG", b)
    put(lib, "originals/2024/02/N.JPG", a)
    suffixed = f"originals/2024/01/N~{sha(b)[:8]}.JPG"
    plan = plan_layout(lib, s, scan(lib))
    assert plan.moves == [("originals/2024/01/N.JPG", suffixed), ("originals/2024/02/N.JPG", "originals/2024/01/N.JPG")]
    assert plan.summary() == CLEAN | {"moves": 2}
    apply_layout(lib, plan)
    assert (lib.root / suffixed).read_bytes() == b and (lib.originals / "2024/01/N.JPG").read_bytes() == a
    assert plan_layout(lib, s, scan(lib)).summary() == CLEAN


def test_path_vacated_by_a_move_is_missing_not_a_conflict(lib):
    a, b = b"imported first elsewhere", b"imported here"
    s = fold([imp(sha(a), name="N.JPG", ts=T1), imp(sha(b), name="N.JPG", ts=T2)])
    put(lib, "originals/2024/01/N.JPG", b)
    plan = plan_layout(lib, s, scan(lib))
    assert plan.missing == [(sha(a), "originals/2024/01/N.JPG")] and plan.conflicts == []
    assert plan.summary() == CLEAN | {"moves": 1, "missing": 1}


def test_swapped_files_are_conflicts_and_stay_put(lib):
    a, b = b"a", b"b"
    s = fold([imp(sha(a), name="A.JPG", ts=T1), imp(sha(b), name="B.JPG", ts=T2)])
    put(lib, "originals/2024/01/A.JPG", b)
    put(lib, "originals/2024/01/B.JPG", a)
    plan = plan_layout(lib, s, scan(lib))
    assert plan.moves == [] and plan.conflicts == [
        ("originals/2024/01/A.JPG", sha(a), sha(b)), ("originals/2024/01/B.JPG", sha(b), sha(a))]
    apply_layout(lib, plan)
    assert (lib.originals / "2024/01/A.JPG").read_bytes() == b


def test_import_with_traversal_folder_places_and_moves_nothing(lib, jpeg):
    put(lib, "originals/2024/01/A.JPG", jpeg)
    s = fold([imp(sha(jpeg), name="A.JPG", folder="../../x")])
    plan = plan_layout(lib, s, scan(lib))
    assert plan.moves == [] and plan.missing == [] and plan.deletes == []
    apply_layout(lib, plan)
    assert (lib.originals / "2024/01/A.JPG").read_bytes() == jpeg and not (lib.root.parent / "x").exists()


def test_plan_paths_outside_the_library_are_not_applied(lib, jpeg):
    put(lib, "originals/2024/01/A.JPG", jpeg)
    victim = lib.root.parent / "victim"
    victim.write_bytes(b"v")
    plan = LayoutPlan(moves=[("originals/2024/01/A.JPG", "../outside/A.JPG"), ("../victim", "originals/v")],
                      deletes=["../victim", "library.json"], missing=[], untracked=[], conflicts=[],
                      delete_shas={"../victim": sha(b"v"), "library.json": sha((lib.root / "library.json").read_bytes())})
    apply_layout(lib, plan)
    assert (lib.originals / "2024/01/A.JPG").read_bytes() == jpeg and not (lib.root.parent / "outside").exists()
    assert victim.read_bytes() == b"v" and (lib.root / "library.json").exists()
    assert not (lib.originals / "v").exists()


def test_copy_verified_leaves_no_partial_on_mismatch(tmp_path):
    src = tmp_path / "s"
    src.write_bytes(b"abc")
    with pytest.raises(HashMismatch):
        copy_verified(src, tmp_path / "d", "0" * 64)
    assert not (tmp_path / "d").exists() and not (tmp_path / "d.photokeep-part").exists()


def test_copy_verified_creates_parents(tmp_path):
    src = tmp_path / "s"
    src.write_bytes(b"abc")
    dst = tmp_path / "x/y/d"
    copy_verified(src, dst, sha(b"abc"))
    assert dst.read_bytes() == b"abc" and list(dst.parent.iterdir()) == [dst]


def test_copy_verified_replaces_a_stale_part(tmp_path):
    src = tmp_path / "s"
    src.write_bytes(b"abc")
    (tmp_path / "d.photokeep-part").write_bytes(b"half of an earlier run")
    copy_verified(src, tmp_path / "d", sha(b"abc"))
    assert (tmp_path / "d").read_bytes() == b"abc" and not (tmp_path / "d.photokeep-part").exists()


def test_copy_verified_never_overwrites(tmp_path):
    src = tmp_path / "s"
    src.write_bytes(b"abc")
    (tmp_path / "d").write_bytes(b"old")
    with pytest.raises(CopyError):
        copy_verified(src, tmp_path / "d", sha(b"abc"))
    assert (tmp_path / "d").read_bytes() == b"old" and not (tmp_path / "d.photokeep-part").exists()


def test_scan_removes_stale_part_files(lib):
    put(lib, "originals/2024/01/A.JPG.photokeep-part", b"half")
    assert scan(lib) == {}
    assert not (lib.root / "originals/2024/01/A.JPG.photokeep-part").exists()


def test_scan_without_cleanup_keeps_part_files(lib):
    put(lib, "originals/2024/01/A.JPG.photokeep-part", b"half")
    assert scan(lib, cleanup=False) == {}
    assert (lib.root / "originals/2024/01/A.JPG.photokeep-part").read_bytes() == b"half"


def test_original_named_like_a_partial_survives_scan_and_copy(lib, jpeg):
    real = put(lib, "originals/2024/01/A.JPG.part", b"a real file")
    src = put(lib, "sources/A.JPG", jpeg)
    copy_verified(src, lib.originals / "2024/01/A.JPG", sha(jpeg))
    assert scan(lib)[sha(b"a real file")] == ["originals/2024/01/A.JPG.part"]
    assert real.read_bytes() == b"a real file"


def test_file_vanishing_mid_scan_is_skipped(lib, monkeypatch):
    gone = put(lib, "originals/2024/01/a", b"a")
    put(lib, "originals/2024/01/b", b"b")
    real_sha = HashCache.sha

    def vanish_first(cache, path, rehash=False):
        gone.unlink(missing_ok=True)
        return real_sha(cache, path, rehash)

    monkeypatch.setattr(HashCache, "sha", vanish_first)
    assert scan(lib) == {sha(b"b"): ["originals/2024/01/b"]}


def test_scan_covers_originals_and_trash_only_with_sorted_paths(lib):
    for rel in ("trash/t/z", "originals/2024/02/b", "originals/2024/01/a", "sources/i/a", "cache/a", "app/a"):
        put(lib, rel, b"same")
    assert scan(lib) == {sha(b"same"): ["originals/2024/01/a", "originals/2024/02/b", "trash/t/z"]}


def test_scan_skips_symlinks(lib):
    (lib.originals / "dangling").symlink_to(lib.root / "nowhere")
    assert scan(lib) == {}


def test_hash_cache_rehashes_only_when_size_or_mtime_changes(lib):
    path = put(lib, "originals/x", b"aaa")
    with HashCache(lib) as cache:
        assert cache.sha(path) == sha(b"aaa")
    st = path.stat()
    path.write_bytes(b"bbb")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    with HashCache(lib) as cache:
        assert cache.sha(path) == sha(b"aaa")
        assert cache.sha(path, rehash=True) == sha(b"bbb")
        path.write_bytes(b"cccc")
        assert cache.sha(path) == sha(b"cccc")
    assert (lib.cache / "files.sqlite").is_file()


def test_symlinked_originals_folder_is_never_written(lib, jpeg, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    lib.originals.rename(elsewhere)
    lib.originals.symlink_to(elsewhere)
    put(lib, "originals/2024/01/A.JPG", jpeg)
    (elsewhere / "empty").mkdir()
    plan = plan_layout(lib, state_with(lib, jpeg, name="A.JPG", trashed=True), scan(lib))
    assert plan.moves == [("originals/2024/01/A.JPG", f"trash/{sha(jpeg)}/A.JPG")]
    apply_layout(lib, plan)
    assert (elsewhere / "2024/01/A.JPG").read_bytes() == jpeg and (elsewhere / "empty").is_dir()
    assert list(lib.trash.iterdir()) == []


def _refuse_moves_to(monkeypatch, name: str, code: int) -> None:
    real = os.replace

    def replace(src, dst):
        if os.path.basename(dst) == name:
            raise OSError(code, os.strerror(code), dst)
        real(src, dst)

    monkeypatch.setattr(os, "replace", replace)


def test_a_move_that_fails_is_listed_and_the_rest_done(lib, monkeypatch):
    for data, name in ((b"first", "A.JPG"), (b"second", "B.JPG")):
        put(lib, f"originals/2024/01/{name}", data)
        state = state_with(lib, data, name=name, trashed=True)
    _refuse_moves_to(monkeypatch, "A.JPG", errno.EINVAL)
    failed = apply_layout(lib, plan_layout(lib, state, scan(lib)))
    assert [path for path, _ in failed] == ["originals/2024/01/A.JPG"] and "Invalid argument" in failed[0][1]
    assert (lib.trash / sha(b"second") / "B.JPG").is_file() and (lib.originals / "2024/01/A.JPG").is_file()


def test_a_full_disk_stops_the_layout(lib, monkeypatch):
    put(lib, "originals/2024/01/A.JPG", b"first")
    state = state_with(lib, b"first", name="A.JPG", trashed=True)
    _refuse_moves_to(monkeypatch, "A.JPG", errno.ENOSPC)
    with pytest.raises(OSError, match="No space"):
        apply_layout(lib, plan_layout(lib, state, scan(lib)))


JUNK = (".DS_Store", "._A.JPG", "Thumbs.db", "desktop.ini")


def test_os_junk_is_never_scanned_moved_or_deleted(lib, jpeg):
    put(lib, "originals/2024/01/A.JPG", jpeg)
    for top in ("originals/2024/01", "trash", f"trash/{sha(jpeg)}"):
        for name in JUNK:
            put(lib, f"{top}/{name}", b"junk")
    state = state_with(lib, jpeg, name="A.JPG", trashed=True)
    assert scan(lib) == {sha(jpeg): ["originals/2024/01/A.JPG"]}
    plan = plan_layout(lib, state, scan(lib))
    assert plan.untracked == [] and plan.deletes == []
    apply_layout(lib, plan)
    for top in ("originals/2024/01", "trash", f"trash/{sha(jpeg)}"):
        assert all((lib.root / top / name).read_bytes() == b"junk" for name in JUNK)

from datetime import UTC, datetime

from photokeep.state import fold
from tests.helpers import T0, T1, T2, T3, T4, ev, imp

FP = ["2024-01-02T03:04:05", "Cam", 4000, 3000]


def test_first_import_creates_live_item():
    e = imp("a", fingerprint=FP, version_of="b", live="IMG_1.MOV")
    it = fold([e]).items["a"]
    assert (it.status, it.folder, it.first_ts, it.imports) == ("live", "2024/01", T0, [e["import"]])
    assert it.files == e["files"] and it.fingerprint == tuple(FP) and it.version_of == "b"
    assert it.edit_of is None and fold([e | {"edit_of": "c"}]).items["a"].edit_of == "c"


def test_later_import_of_live_item_appends_import_id():
    e1, e2 = imp("a", ts=T0), imp("a", ts=T1)
    it = fold([e1, e2]).items["a"]
    assert it.imports == [e1["import"], e2["import"]] and it.first_ts == T0


def test_import_of_trashed_item_is_ignored():
    s = fold([imp("a"), ev("trash", "a", ts=T1), imp("a", ts=T2)])
    assert s.items["a"].status == "trashed" and len(s.items["a"].imports) == 1


def test_import_with_blocked_fingerprint_is_ignored():
    s = fold([imp("a", fingerprint=FP), ev("trash", "a", ts=T1), imp("b", ts=T2, fingerprint=FP)])
    assert "b" not in s.items


def test_trash_sets_trashed_at_and_blocked():
    it = fold([imp("a"), ev("trash", "a", ts=T1, reason="dup")]).items["a"]
    assert (it.status, it.trashed_at, it.blocked, it.trash_reason) == ("trashed", T1, True, "dup")


def test_restore_after_trash_is_live():
    s = fold([imp("a"), ev("trash", "a", ts=T1), ev("restore", "a", ts=T2)])
    assert s.items["a"].status == "live" and not s.items["a"].blocked and s.items["a"].trashed_at is None


def test_purge_is_final_against_later_restore():
    s = fold([imp("a"), ev("trash", "a", ts=T1), ev("purge", "a", ts=T2), ev("restore", "a", ts=T3)])
    assert s.items["a"].status == "purged" and s.items["a"].blocked


def test_purge_of_live_item_purges_it():
    assert fold([imp("a"), ev("purge", "a", ts=T1)]).items["a"].status == "purged"


def test_reimport_of_purged_is_ignored_until_readmit():
    base = [imp("a", ts=T0), ev("trash", "a", ts=T1), ev("purge", "a", ts=T2)]
    assert fold(base + [imp("a", ts=T3)]).items["a"].status == "purged"
    s = fold(base + [ev("readmit", "a", ts=T3), imp("a", ts=T4)])
    assert s.items["a"].status == "live" and not s.items["a"].blocked and s.items["a"].first_ts == T4


def test_tombstone_blocks_unknown_item():
    s = fold([ev("purge", "z" * 64, ts=T1, name="IMG_1.HEIC")])
    assert s.is_blocked("z" * 64) and s.tombstones["z" * 64] == "IMG_1.HEIC"
    assert "z" * 64 not in s.items
    assert "z" * 64 not in fold([ev("purge", "z" * 64, ts=T1), imp("z" * 64, ts=T2)]).items


def test_readmit_clears_tombstone():
    s = fold([ev("purge", "z" * 64, ts=T1), ev("readmit", "z" * 64, ts=T2), imp("z" * 64, ts=T3)])
    assert not s.tombstones and s.items["z" * 64].status == "live"


def test_is_blocked_by_item_id_or_fingerprint():
    s = fold([imp("a", fingerprint=FP), imp("b"), ev("trash", "a", ts=T1)])
    assert s.is_blocked("a") and s.is_blocked("x", tuple(FP))
    assert not s.is_blocked("b") and not s.is_blocked("x") and not s.is_blocked("x", None)


def test_favorite_and_hide_apply_to_any_item():
    s = fold([imp("a"), ev("trash", "a", ts=T1), ev("favorite", "a", ts=T2), ev("hide", "a", ts=T2),
              ev("unfavorite", "a", ts=T3), ev("unhide", "a", ts=T3), ev("favorite", "a", ts=T4)])
    assert s.items["a"].favorite and not s.items["a"].hidden


def test_albums_add_remove():
    s = fold([imp("a"), ev("album.add", "a", ts=T1, album="Trip", kind="album"),
              ev("album.add", "a", ts=T1, album="Trip", kind="memory"),
              ev("album.remove", "a", ts=T2, album="Trip", kind="memory")])
    assert s.items["a"].albums == {("album", "Trip")}


def test_album_rename_hits_every_item_of_that_kind():
    s = fold([imp("a"), imp("b"), ev("album.add", "a", ts=T1, album="Trip", kind="album"),
              ev("album.add", "b", ts=T1, album="Trip", kind="memory"),
              ev("album.rename", ts=T2, album="Trip", to="Spain", kind="album")])
    assert s.items["a"].albums == {("album", "Spain")} and s.items["b"].albums == {("memory", "Trip")}


def test_copy_name_fills_copies():
    assert fold([ev("copy.name", ts=T1, copy="c1", name="laptop")]).copies == {"c1": "laptop"}


def test_unknown_ops_are_ignored():
    assert fold([imp("a"), ev("future.op", "a", ts=T1)]).items["a"].status == "live"


def test_live_lists_only_live_items():
    s = fold([imp("a"), imp("b"), ev("trash", "b", ts=T1)])
    assert [i.id for i in s.live()] == ["a"]


def test_due_purges_after_30_days():
    s = fold([imp("b"), imp("a"), ev("trash", "a", ts="2026-01-01T00:00:00.000000Z"),
              ev("trash", "b", ts="2026-01-01T00:00:00.000000Z")])
    assert s.due_purges(datetime(2026, 1, 31, tzinfo=UTC)) == ["a", "b"]
    assert s.due_purges(datetime(2026, 1, 30, tzinfo=UTC)) == []


def test_shared_fingerprint_stays_blocked_while_any_holder_is_blocked():
    s = fold([imp("a", fingerprint=FP), imp("b", ts=T1, fingerprint=FP), ev("trash", "a", ts=T2),
              imp("c", ts=T3, fingerprint=FP)])
    assert s.is_blocked("x", tuple(FP)) and "c" not in s.items


def test_repeat_import_of_live_version_survives_trashed_twin():
    s = fold([imp("a", fingerprint=FP), imp("b", ts=T1, fingerprint=FP), ev("trash", "a", ts=T2),
              imp("b", ts=T3, fingerprint=FP)])
    assert len(s.items["b"].imports) == 2


def test_trash_after_purge_stays_purged():
    s = fold([imp("a"), ev("purge", "a", ts=T1), ev("trash", "a", ts=T2)])
    assert s.items["a"].status == "purged" and s.items["a"].trashed_at is None


def test_trash_of_unknown_item_changes_nothing():
    s = fold([ev("purge", "z", ts=T1, name="n"), ev("trash", "z", ts=T2), ev("trash", "y", ts=T2)])
    assert s.items == {} and s.tombstones == {"z": "n"}


def test_purged_and_restored_items_are_not_due():
    old = "2026-01-01T00:00:00.000000Z"
    s = fold([imp("a"), imp("b"), ev("trash", "a", ts=old), ev("purge", "a", ts=T1),
              ev("trash", "b", ts=old), ev("restore", "b", ts=T1)])
    assert s.due_purges(datetime(2027, 1, 1, tzinfo=UTC)) == []

import unicodedata

from photokeep.paths import assign, safe_name, valid_folder, valid_id
from photokeep.state import fold
from tests.helpers import T1, T2, T3, ev, imp

A, B, C = "a" * 64, "b" * 64, "c" * 64


def test_clash_by_case_gets_suffix_for_later_import():
    p = assign(fold([imp(A, name="IMG_1.HEIC", ts=T1), imp(B, name="img_1.heic", ts=T2)]))
    assert p[A]["main"] == "originals/2024/01/IMG_1.HEIC"
    assert p[B]["main"] == f"originals/2024/01/img_1~{'b' * 8}.heic"


def test_nfd_and_nfc_names_clash():
    nfd = unicodedata.normalize("NFD", "Pagaré.pdf")
    p = assign(fold([imp(A, name="Pagaré.pdf", ts=T1), imp(B, name=nfd, ts=T2)]))
    assert p[B]["main"].endswith(f"Pagaré~{'b' * 8}.pdf")


def test_forbidden_characters_replaced():
    assert safe_name('08:26 dallas?.pdf') == "08_26 dallas_.pdf"


def test_safe_name_control_chars_trailing_dots_and_empty():
    assert safe_name("a\x00b\x7f.txt.. ") == "a_b_.txt"
    assert safe_name("...") == "_"


def test_trashed_item_keeps_its_name_inside_trash():
    p = assign(fold([imp(A, name="X.JPG", ts=T1), ev("trash", A, ts=T2)]))
    assert p[A]["main"] == f"trash/{A}/X.JPG"


def test_trashed_item_still_holds_its_name():
    s = fold([imp(A, name="X.JPG", ts=T1), ev("trash", A, ts=T2), imp(B, name="x.jpg", ts=T3)])
    assert assign(s)[B]["main"] == f"originals/2024/01/x~{'b' * 8}.jpg"


def test_purged_item_is_skipped_and_name_not_reused_in_run():
    s = fold([imp(A, name="X.JPG", ts=T1), ev("trash", A, ts=T2), ev("purge", A, ts=T3)])
    assert assign(s) == {}


def test_assignment_is_independent_of_event_arrival_order():
    evs = [imp(A, name="N.JPG", ts=T1), imp(B, name="N.JPG", ts=T2)]
    assert assign(fold(evs)) == assign(fold(list(reversed(evs))))


def test_live_clip_shares_folder_namespace_with_main_files():
    s = fold([imp(A, name="IMG_1.JPG", live="IMG_1.MOV", ts=T1), imp(B, name="img_1.mov", ts=T2)])
    p = assign(s)
    assert p[A] == {"main": "originals/2024/01/IMG_1.JPG", "live": "originals/2024/01/IMG_1.MOV"}
    assert p[B]["main"] == f"originals/2024/01/img_1~{'b' * 8}.mov"


def test_dotless_name_and_undated_folder():
    p = assign(fold([imp(A, name="README", folder="undated", ts=T1), imp(B, name="readme", folder="undated", ts=T2)]))
    assert p[A]["main"] == "originals/undated/README"
    assert p[B]["main"] == f"originals/undated/readme~{'b' * 8}"


def test_suffixed_name_that_is_taken_falls_back_to_full_id():
    taken = f"X~{B[:8]}.JPG"
    s = fold([imp(A, name="X.JPG", ts=T1), imp(C, name=taken, ts=T2), imp(B, name="x.jpg", ts=T3)])
    p = assign(s)
    assert p[B]["main"] == f"originals/2024/01/x~{B}.jpg"
    assert len({v["main"].casefold() for v in p.values()}) == 3


def test_safe_name_never_ends_in_the_partial_suffix():
    assert safe_name("A.JPG.photokeep-part") == "A.JPG.photokeep-part_"
    assert safe_name("A.JPG.PhotoKeep-Part.") == "A.JPG.PhotoKeep-Part_"
    assert safe_name("A.JPG.part") == "A.JPG.part"


def test_valid_folder_and_id():
    assert valid_folder("2024/01") and valid_folder("undated")
    for bad in ("../../x", "2024/1", "2024/01/..", "/2024/01", "２０２４/01", "2024/01\n", "", None, 2024):
        assert not valid_folder(bad), bad
    assert valid_id(A)
    for bad in ("A" * 64, "a" * 63, "../" + "a" * 61, "g" * 64, None):
        assert not valid_id(bad), bad


def test_assign_skips_items_with_unsafe_folder_or_id():
    s = fold([imp(A, folder="../../x", ts=T1), imp("../../etc/" + "a" * 54, ts=T2), imp(B, ts=T3)])
    assert list(assign(s)) == [B]

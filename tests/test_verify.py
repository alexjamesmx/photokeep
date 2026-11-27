import json
import os
import random
import re
from datetime import UTC, datetime

import pytest

from photokeep.cli import main
from photokeep.library import LibraryError, init_library, locked
from photokeep.sync import run
from photokeep.verify import repair, verify
from tests.helpers import add_item, files_of, mark, no_part_files, put, snapshot

NOW = datetime(2026, 1, 2, tzinfo=UTC)
MAIN = "originals/2024/01/A.JPG"


def synced(a, b, data=b"photo"):
    item = add_item(a, data, name="A.JPG")
    run(a, b, NOW)
    return item


def test_healthy_copy_is_ok(a):
    add_item(a, b"photo", name="A.JPG")
    r = verify(a)
    assert r.ok and r.checked == 1
    assert r.to_json() == {"ok": True, "checked": 1, "missing": [], "corrupt": [], "untracked": []}


def test_bit_flip_is_corrupt_even_with_unchanged_size_and_mtime(a):
    add_item(a, b"photo", name="A.JPG")
    verify(a)  # fills the hash cache
    path = a.root / MAIN
    stat = path.stat()
    path.write_bytes(b"phOto")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    r = verify(a)
    assert r.corrupt == [MAIN] and r.missing == [] and r.untracked == [] and not r.ok


def test_deleted_file_is_missing(a):
    add_item(a, b"photo", name="A.JPG")
    (a.root / MAIN).unlink()
    r = verify(a)
    assert r.missing == [MAIN] and r.checked == 0 and not r.ok


def test_untracked_files_are_listed_and_trashed_items_are_expected(a):
    x = add_item(a, b"photo", name="A.JPG")
    mark(a, "trash", x)
    put(a, "originals/2024/01/stray.jpg", b"stray")
    r = verify(a)
    assert r.missing == [f"trash/{x}/A.JPG"] and r.untracked == ["originals/2024/01/stray.jpg"]


def test_verify_writes_nothing_but_the_cache(a):
    add_item(a, b"photo", name="A.JPG")
    put(a, "originals/2024/01/x.photokeep-part", b"half")
    before = snapshot(a)
    verify(a)
    assert snapshot(a) == before


def test_sample_hashes_only_n_files(a):
    for i in range(5):
        add_item(a, f"p{i}".encode(), name=f"{i}.JPG")
    assert verify(a, sample=2, rng=random.Random(1)).checked == 2
    assert verify(a, sample=99).checked == 5


def test_sample_finds_corruption_when_it_picks_the_file(a):
    add_item(a, b"photo", name="A.JPG")
    (a.root / MAIN).write_bytes(b"rotten")
    assert verify(a, sample=1).corrupt == [MAIN]


def test_repair_restores_corrupt_and_missing_and_keeps_the_bad_bytes(a, b):
    synced(a, b)
    add_item(a, b"second", name="B.JPG")
    run(a, b, NOW)
    (a.root / MAIN).write_bytes(b"rotten")
    (a.root / "originals/2024/01/B.JPG").unlink()
    r = repair(a, b)
    assert r.ok and r.restore == [MAIN, "originals/2024/01/B.JPG"] and files_of(a) == files_of(b)
    [kept] = [p for p in (a.root / "quarantine").rglob("*") if p.is_file()]
    assert kept.read_bytes() == b"rotten" and kept.parents[3] == a.root / r.quarantine
    run_folder, *rel = kept.relative_to(a.root / "quarantine").parts
    assert rel == ["originals", "2024", "01", "A.JPG"] and re.fullmatch(r"\d{8}T\d{6}Z-\w+", run_folder)
    assert repair(a, b).quarantine is None  # nothing left to replace


def test_repair_reports_what_the_other_copy_lacks(a, b):
    synced(a, b)
    (a.root / MAIN).unlink()
    (b.root / MAIN).unlink()
    r = repair(a, b)
    assert r.after.missing == r.no_source == [MAIN] and not r.ok


def test_repair_does_not_use_a_corrupt_source(a, b):
    synced(a, b)
    (a.root / MAIN).unlink()
    (b.root / MAIN).write_bytes(b"rotten")
    r = repair(a, b)
    assert r.after.missing == r.no_source == [MAIN] and not (a.root / MAIN).exists()


def test_corrupt_file_without_a_good_copy_stays_where_it_is(a, b):
    synced(a, b)
    (b.root / MAIN).unlink()  # the drive lost it
    (a.root / MAIN).write_bytes(b"phoTo")  # and the laptop's copy has one bad byte
    r = repair(a, b)
    assert (a.root / MAIN).read_bytes() == b"phoTo" and not (a.root / "quarantine").exists()
    assert (r.no_source, r.after.corrupt, r.quarantine, r.ok) == ([MAIN], [MAIN], None, False)


def test_corrupt_file_stays_when_the_other_copy_turns_out_rotten(a, b):
    synced(a, b)
    verify(b)  # b's hash cache now vouches for its file
    path = b.root / MAIN
    st = path.stat()
    path.write_bytes(b"rOtten"[:len(b"photo")])
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    (a.root / MAIN).write_bytes(b"phoTo")
    r = repair(a, b)
    assert (a.root / MAIN).read_bytes() == b"phoTo" and not (a.root / "quarantine").exists()
    assert [p for p, _ in r.failed] == [MAIN] and "SHA-256" in r.failed[0][1] and no_part_files(a)


def test_repair_dry_run_changes_nothing_and_says_what_it_would_do(a, b, capsys):
    synced(a, b)
    add_item(a, b"second", name="B.JPG")
    run(a, b, NOW)
    (a.root / MAIN).write_bytes(b"rotten")
    (a.root / "originals/2024/01/B.JPG").unlink()
    (b.root / "originals/2024/01/B.JPG").unlink()
    before = snapshot(a, b)
    r = repair(a, b, dry_run=True)
    assert snapshot(a, b) == before and not (a.root / "quarantine").exists()
    assert (r.restore, r.no_source, r.ok) == ([MAIN], ["originals/2024/01/B.JPG"], False)
    assert main(["repair", str(a.root), "--from", str(b.root), "--dry-run", "--json"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert (out["dry_run"], out["restore"], out["corrupt"], out["quarantine"]) == (True, [MAIN], [MAIN], None)
    assert main(["repair", str(a.root), "--from", str(b.root), "--dry-run"]) == 2
    assert "dry run: nothing changed" in capsys.readouterr().out and snapshot(a, b) == before


def test_repair_refuses_other_library_same_copy_and_busy_copy(a, b, tmp_path):
    other = init_library(tmp_path / "X", "x")
    with pytest.raises(LibraryError):
        repair(a, other)
    with pytest.raises(LibraryError):
        repair(a, a)
    with locked(b), pytest.raises(LibraryError):
        repair(a, b)


def test_cli_verify_and_repair(a, b, capsys):
    synced(a, b)
    assert main(["verify", str(a.root)]) == 0
    (a.root / MAIN).write_bytes(b"rotten")
    capsys.readouterr()
    assert main(["verify", str(a.root), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["corrupt"] == [MAIN]
    assert main(["verify", str(a.root), "--sample", "1"]) == 2
    assert main(["repair", str(a.root), "--from", str(b.root)]) == 0
    assert main(["verify", str(a.root)]) == 0
    (b.root / MAIN).unlink()
    (a.root / MAIN).unlink()
    assert main(["repair", str(a.root), "--from", str(b.root)]) == 2


def test_cli_errors_exit_1(tmp_path, capsys):
    assert main(["verify", str(tmp_path / "nope")]) == 1
    assert "photokeep:" in capsys.readouterr().err


def test_cli_verify_and_repair_with_relative_paths(a, b, monkeypatch):
    synced(a, b)
    (a.root / MAIN).unlink()
    monkeypatch.chdir(a.root.parent)
    assert main(["verify", "B"]) == 0
    assert main(["repair", "A", "--from", "B"]) == 0
    assert main(["verify", "A"]) == 0 and files_of(a) == files_of(b)

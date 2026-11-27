import json
import os
import random
from datetime import UTC, datetime

import pytest

from photokeep.cli import main
from photokeep.library import LibraryError, init_library, locked
from photokeep.sync import run
from photokeep.verify import repair, verify
from tests.helpers import add_item, files_of, mark, put, snapshot

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
    assert repair(a, b).ok
    assert files_of(a) == files_of(b)
    [kept] = [p for p in (a.root / "cache/quarantine").rglob("*") if p.is_file()]
    assert kept.read_bytes() == b"rotten"
    assert kept.relative_to(a.root / "cache/quarantine").parts[1:] == ("originals", "2024", "01", "A.JPG")
    assert len(kept.relative_to(a.root / "cache/quarantine").parts[0]) == 16


def test_repair_reports_what_the_other_copy_lacks(a, b):
    synced(a, b)
    (a.root / MAIN).unlink()
    (b.root / MAIN).unlink()
    r = repair(a, b)
    assert r.missing == [MAIN] and not r.ok


def test_repair_does_not_use_a_corrupt_source(a, b):
    synced(a, b)
    (a.root / MAIN).unlink()
    (b.root / MAIN).write_bytes(b"rotten")
    r = repair(a, b)
    assert r.missing == [MAIN] and not (a.root / MAIN).exists()


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

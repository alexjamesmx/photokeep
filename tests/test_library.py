import json

import pytest

from photokeep.cli import main
from photokeep.library import LibraryError, init_copy, init_library, open_library


def test_init_creates_layout(tmp_path):
    lib = init_library(tmp_path / "L", "laptop")
    for d in ("originals", "sources", "log", "trash", "cache", "app"):
        assert (lib.root / d).is_dir()
    assert json.loads((lib.root / "library.json").read_text())["format"] == 1
    assert open_library(lib.root) == lib


def test_copy_of_shares_library_id(tmp_path):
    a = init_library(tmp_path / "A", "laptop")
    b = init_copy(tmp_path / "B", "hdd-1", of=a)
    assert b.library_id == a.library_id and b.copy_id != a.copy_id
    assert open_library(b.root).copy_name == "hdd-1"


def test_log_path_defaults_to_own_copy(tmp_path):
    lib = init_library(tmp_path / "L", "x")
    assert lib.log_path() == lib.root / "log" / f"{lib.copy_id}.jsonl"
    assert lib.log_path("abc") == lib.root / "log" / "abc.jsonl"


def test_open_rejects_newer_format(tmp_path):
    lib = init_library(tmp_path / "L", "x")
    data = json.loads((lib.root / "library.json").read_text())
    data["format"] = 2
    (lib.root / "library.json").write_text(json.dumps(data))
    with pytest.raises(LibraryError, match="newer"):
        open_library(lib.root)


def test_init_refuses_non_empty_dir(tmp_path):
    (tmp_path / "L").mkdir()
    (tmp_path / "L" / "x").write_text("x")
    with pytest.raises(LibraryError, match="not empty"):
        init_library(tmp_path / "L", "x")


def test_init_accepts_empty_existing_dir(tmp_path):
    (tmp_path / "L").mkdir()
    assert init_library(tmp_path / "L", "x").root == tmp_path / "L"


def test_open_rejects_non_library(tmp_path):
    with pytest.raises(LibraryError, match="not a Photokeep library"):
        open_library(tmp_path)


def test_no_temp_files_left(tmp_path):
    lib = init_library(tmp_path / "L", "x")
    assert sorted(p.name for p in lib.root.iterdir() if p.is_file()) == ["copy.json", "library.json"]


def test_cli_init_and_copy_of(tmp_path, capsys):
    assert main(["init", str(tmp_path / "A"), "--name", "laptop", "--json"]) == 0
    a = json.loads(capsys.readouterr().out)
    assert main(["init", str(tmp_path / "B"), "--name", "hdd", "--copy-of", str(tmp_path / "A"), "--json"]) == 0
    b = json.loads(capsys.readouterr().out)
    assert a["library_id"] == b["library_id"] and a["copy_id"] != b["copy_id"]
    assert b["root"] == str(tmp_path / "B")


def test_cli_init_plain_output(tmp_path, capsys):
    assert main(["init", str(tmp_path / "A"), "--name", "laptop"]) == 0
    out = capsys.readouterr().out
    assert str(tmp_path / "A") in out and open_library(tmp_path / "A").library_id in out


def test_cli_init_error_exits_1(tmp_path, capsys):
    (tmp_path / "A").mkdir()
    (tmp_path / "A" / "x").write_text("x")
    assert main(["init", str(tmp_path / "A"), "--name", "n"]) == 1
    assert "not empty" in capsys.readouterr().err

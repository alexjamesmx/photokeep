import errno
import json
import os
import shutil

import pytest

from photokeep.cli import main
from photokeep.library import LibraryError, init_copy, init_library, locked, open_library


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


@pytest.mark.parametrize("name", ["originals", "trash", "sources", "log", "app", "cache"])
def test_open_refuses_a_symlinked_folder(tmp_path, name):
    lib = init_library(tmp_path / "L", "x")
    (lib.root / name).rename(tmp_path / "elsewhere")
    (lib.root / name).symlink_to(tmp_path / "elsewhere")
    with pytest.raises(LibraryError, match="symlink"):
        open_library(lib.root)


@pytest.mark.parametrize("name", ["originals", "trash", "sources", "log", "app", "cache"])
def test_open_refuses_a_file_in_place_of_a_folder(tmp_path, name):
    lib = init_library(tmp_path / "L", "x")
    shutil.rmtree(lib.root / name)
    (lib.root / name).write_bytes(b"")
    with pytest.raises(LibraryError, match="not a folder"):
        open_library(lib.root)


def test_open_needs_originals_but_not_cache_or_app(tmp_path):
    lib = init_library(tmp_path / "L", "x")
    shutil.rmtree(lib.cache)
    shutil.rmtree(lib.app)  # a cut-off app update leaves none until the next sync restores it
    assert open_library(lib.root) == lib
    shutil.rmtree(lib.originals)
    with pytest.raises(LibraryError, match="not a folder"):
        open_library(lib.root)


def test_lock_recreates_a_deleted_cache(lib):
    shutil.rmtree(lib.cache)
    with locked(lib):
        assert (lib.cache / "lock").is_file()


def test_lock_refuses_a_symlinked_cache_or_lock(lib, tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"keep")
    (lib.cache / "lock").symlink_to(target)
    with pytest.raises(OSError), locked(lib):
        pass
    shutil.rmtree(lib.cache)
    lib.cache.symlink_to(tmp_path)
    with pytest.raises(LibraryError, match="symlink"), locked(lib):
        pass
    assert target.read_bytes() == b"keep" and not (tmp_path / "lock").exists()


@pytest.mark.parametrize("code", [errno.ENOTSUP, errno.ENOLCK, errno.EINVAL])
def test_lock_is_skipped_where_the_filesystem_has_none(lib, monkeypatch, code):
    fcntl = pytest.importorskip("fcntl")

    def unsupported(fd, flags):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(fcntl, "flock", unsupported)
    with locked(lib):
        pass


def test_lock_passes_on_other_errors(lib, monkeypatch):
    fcntl = pytest.importorskip("fcntl")

    def broken(fd, flags):
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(fcntl, "flock", broken)
    with pytest.raises(OSError), locked(lib):
        pass

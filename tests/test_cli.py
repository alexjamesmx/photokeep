import json
import shutil
import tomllib
import zipfile
from pathlib import Path

import pytest

from photokeep import __version__
from photokeep.cli import main
from tests.helpers import jpeg_bytes

needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool is not installed")

ROOT = Path(__file__).resolve().parent.parent


def test_version_flag(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"photokeep {__version__}"


def test_version_has_one_source():
    # sync moves an app/ only to a copy whose version is lower, so a second, stale number would hide updates
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "version" not in project["project"] and project["project"]["dynamic"] == ["version"]
    assert project["tool"]["hatch"]["version"]["path"] == "photokeep/__init__.py"
    assert __version__ == "0.3.1"


def _takeout(path: Path, *, sidecars: bool = True) -> Path:
    # a year folder with two photos; `sidecars=False` leaves the second one without its sidecar
    year = "Takeout/Google Photos/Photos from 2024"
    meta = json.dumps({"title": "A.jpg", "photoTakenTime": {"timestamp": "1709632800"}}).encode()
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{year}/A.jpg", jpeg_bytes((10, 20, 30)))
        zf.writestr(f"{year}/A.jpg.supplemental-metadata.json", meta)
        zf.writestr(f"{year}/B.jpg", jpeg_bytes((90, 20, 30)))
        if sidecars:
            zf.writestr(f"{year}/B.jpg.supplemental-metadata.json", meta.replace(b"A.jpg", b"B.jpg"))
    return path


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and p.relative_to(root).parts[0] in ("originals", "trash", "sources", "log")}


@needs_exiftool
def test_import_dry_run_prints_the_summary_and_writes_nothing(lib, tmp_path, capsys):
    zip_ = _takeout(tmp_path / "t.zip")
    before = _tree(lib.root)
    assert main(["import", "takeout", str(zip_), "--into", str(lib.root), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "imported: 2" in out and "dry run: nothing changed" in out
    assert _tree(lib.root) == before
    assert not (lib.cache / "import").exists() or not any((lib.cache / "import").iterdir())


@needs_exiftool
def test_import_json_has_the_summary_keys_and_a_real_run_imports(lib, tmp_path, capsys):
    zip_ = _takeout(tmp_path / "t.zip")
    assert main(["import", "takeout", str(zip_), "--into", str(lib.root), "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert list(got) == ["imported", "versions", "edits", "live_clips", "already_in_library", "duplicates_in_source",
                         "skipped_deleted", "trashed", "album_links", "favorites", "bytes", "problems"]
    assert got["imported"] == 2 and got["problems"] == []
    assert sorted(p.name for p in (lib.root / "originals/2024/03").iterdir()) == ["A.jpg", "B.jpg"]


@needs_exiftool
def test_import_exits_2_and_lists_every_problem(lib, tmp_path, capsys):
    zip_ = _takeout(tmp_path / "t.zip", sidecars=False)
    assert main(["import", "takeout", str(zip_), "--into", str(lib.root)]) == 2
    assert "Google Photos/Photos from 2024/B.jpg: no sidecar" in capsys.readouterr().out


@needs_exiftool
def test_import_folder_adapter(lib, tmp_path, capsys):
    src = tmp_path / "pics"
    src.mkdir()
    (src / "x.jpg").write_bytes(jpeg_bytes())
    assert main(["import", "folder", str(src), "--into", str(lib.root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["imported"] == 1


def test_import_without_exiftool_is_a_one_line_error(lib, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda *_: None)
    assert main(["import", "takeout", str(_takeout(tmp_path / "t.zip")), "--into", str(lib.root)]) == 1
    out, err = capsys.readouterr()
    assert out == "" and err.startswith("photokeep: exiftool is required") and err.count("\n") == 1


def test_import_refuses_what_it_cannot_read(lib, tmp_path, capsys):
    assert main(["import", "takeout", str(tmp_path / "missing.zip"), "--into", str(lib.root)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("photokeep: ") and err.count("\n") == 1


def test_import_into_something_that_is_not_a_copy(tmp_path, capsys):
    assert main(["import", "takeout", str(_takeout(tmp_path / "t.zip")), "--into", str(tmp_path)]) == 1
    assert capsys.readouterr().err.startswith("photokeep: ")

import tomllib
from pathlib import Path

from photokeep import __version__
from photokeep.cli import main

ROOT = Path(__file__).resolve().parent.parent


def test_version_flag(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"photokeep {__version__}"


def test_version_has_one_source():
    # sync moves an app/ only to a copy whose version is lower, so a second, stale number would hide updates
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "version" not in project["project"] and project["project"]["dynamic"] == ["version"]
    assert project["tool"]["hatch"]["version"]["path"] == "photokeep/__init__.py"
    assert __version__ == "0.2.2"

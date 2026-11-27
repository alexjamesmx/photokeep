import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import photokeep
from photokeep.appcopy import install
from photokeep.cli import main
from photokeep.library import locked
from tests.helpers import put

PACKAGE = Path(photokeep.__file__).parent
SH = '#!/bin/sh\ncd "$(dirname "$0")" && PYTHONPATH=app exec python3 -m photokeep serve .\n'
BAT = b'cd /d "%~dp0"\r\nset PYTHONPATH=app\r\npy -3 -m photokeep serve .\r\n'


def files(top: Path) -> list[str]:
    return sorted(p.relative_to(top).as_posix() for p in top.rglob("*") if p.is_file())


def test_install_copies_package_and_launchers(lib):
    install(lib)
    assert (lib.root / "app/photokeep/__init__.py").exists() and (lib.root / "Open Photos.sh").exists()


def test_app_copy_is_the_package_without_caches_or_links(lib):
    install(lib)
    want = [f for f in files(PACKAGE) if "__pycache__" not in f]
    assert files(lib.app / "photokeep") == want and "ui/app.js" in want and "launchers/Open Photos.bat" in want
    assert not any(p.is_symlink() for p in lib.app.rglob("*")) and not any(lib.cache.glob("app-update-*"))


def test_install_replaces_an_older_app_whole(lib):
    put(lib, "app/photokeep/stale.py", b"old")
    install(lib)
    assert not (lib.app / "photokeep/stale.py").exists() and (lib.app / "photokeep/server.py").is_file()


def test_launchers_start_the_copied_app(lib):
    install(lib)
    assert (lib.root / "Open Photos.sh").read_text() == (lib.root / "Open Photos.command").read_text() == SH
    assert (lib.root / "Open Photos.bat").read_bytes() == BAT
    for name in ("Open Photos.sh", "Open Photos.command"):
        assert os.stat(lib.root / name).st_mode & stat.S_IXUSR


def test_launcher_in_place_of_a_symlink_is_replaced_not_written_through(lib, tmp_path):
    target = tmp_path / "elsewhere.sh"
    target.write_text("keep")
    (lib.root / "Open Photos.sh").symlink_to(target)
    install(lib)
    assert target.read_text() == "keep" and (lib.root / "Open Photos.sh").read_text() == SH
    assert not (lib.root / "Open Photos.sh").is_symlink() and not any(lib.root.glob(".*.tmp"))


def test_copied_app_runs_without_installed_packages(lib):
    install(lib)
    env = {**os.environ, "PYTHONPATH": "app"}
    # -S: no site-packages, so only the copy and the standard library can be imported
    run = [sys.executable, "-S", "-c", "import photokeep.cli, photokeep.server; print(photokeep.__file__)"]
    out = subprocess.run(run, cwd=lib.root, env=env, capture_output=True, text=True, check=True).stdout
    assert Path(out.strip()).resolve() == (lib.app / "photokeep/__init__.py").resolve()


def test_cli_init_installs_the_app(tmp_path):
    assert main(["init", str(tmp_path / "A"), "--name", "laptop"]) == 0
    assert main(["init", str(tmp_path / "B"), "--name", "hdd", "--copy-of", str(tmp_path / "A")]) == 0
    for root in (tmp_path / "A", tmp_path / "B"):
        assert (root / "app/photokeep/__init__.py").is_file() and (root / "Open Photos.bat").is_file()


def test_cli_app_install_replaces_a_copys_app_with_this_one(lib, capsys):
    put(lib, "app/photokeep/__init__.py", b'__version__ = "0.0.1"\n')
    (lib.root / "Open Photos.sh").unlink(missing_ok=True)
    assert main(["app", "install", str(lib.root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"root": str(lib.root), "version": photokeep.__version__}
    assert (lib.app / "photokeep/__init__.py").read_bytes() == (PACKAGE / "__init__.py").read_bytes()
    assert (lib.root / "Open Photos.sh").read_text() == SH


def test_cli_app_install_waits_for_a_busy_copy(lib, capsys):
    put(lib, "app/photokeep/__init__.py", b'__version__ = "0.0.1"\n')
    with locked(lib):
        assert main(["app", "install", str(lib.root)]) == 1
    assert "busy" in capsys.readouterr().err
    assert (lib.app / "photokeep/__init__.py").read_bytes() == b'__version__ = "0.0.1"\n'

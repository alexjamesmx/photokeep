"""The copy of the app every library carries in app/, so a drive opens its photos on any computer with Python."""
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from photokeep.library import Library, LibraryError

PACKAGE = Path(__file__).parent
LAUNCHERS = PACKAGE / "launchers"
WORK = "app-update-"  # prefix of the work folders under cache/ while app/ is swapped


def refuse_links(top: Path) -> None:
    """Raise unless top holds only plain folders and files: a symlink in an app could run code from anywhere."""
    paths = [top] + [Path(folder) / name for folder, dirs, files in os.walk(top) for name in dirs + files]
    for path in paths:
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise LibraryError(f"{path} is a symlink or a special file; the app is not copied")


def replace(lib: Library, fill: Callable[[Path], object]) -> None:
    """Swap in a new app/ whose contents fill writes into the (not yet existing) folder it is given.

    The new app is built and checked under cache/ first, and the swap is two renames: a crash leaves the
    old app, the new one, or the old one in a work folder for restore() to put back.
    """
    work = Path(tempfile.mkdtemp(prefix=WORK, dir=lib.cache_dir()))
    try:
        fill(work / "new")
        refuse_links(work / "new")  # the source may have changed since it was checked
    except BaseException:
        shutil.rmtree(work)
        raise
    if os.path.lexists(lib.app):
        os.replace(lib.app, work / "old")
    os.replace(work / "new", lib.app)
    shutil.rmtree(work)


def restore(lib: Library) -> None:
    """Clear up after an app swap that was cut off: put app/ back if it is gone, then drop the work folders."""
    for work in sorted(lib.cache_dir().glob(WORK + "*")):
        if work.is_symlink() or not work.is_dir():
            continue
        old = work / "old"
        if not os.path.lexists(lib.app) and old.is_dir() and not old.is_symlink():
            os.replace(old, lib.app)
        shutil.rmtree(work)


def install(lib: Library) -> None:
    """Put the running photokeep package in lib's app/ and the launchers that start it at lib's root."""
    replace(lib, lambda new: shutil.copytree(PACKAGE, new / "photokeep", symlinks=True,
                                             ignore=shutil.ignore_patterns("__pycache__")))
    for launcher in sorted(LAUNCHERS.iterdir()):
        target = lib.root / launcher.name
        shutil.copyfile(launcher, target)
        if launcher.suffix != ".bat":
            try:
                os.chmod(target, 0o755)
            except PermissionError:  # exFAT and FAT keep no modes; the mount options decide
                pass

"""Sync of sources/ between two copies: import folders are write-once, so files are only added."""
import os
import re
from pathlib import Path
from typing import NamedTuple

from photokeep import fsops
from photokeep.hashcache import file_sha256
from photokeep.library import Library
from photokeep.paths import PART

_IMPORT_ID = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9]+-[0-9a-f]+$")


class Plan(NamedTuple):
    a_to_b: dict[str, str]  # path under sources/ -> sha of the file on a
    b_to_a: dict[str, str]
    conflicts: list[str]


class _Tree(NamedTuple):
    files: dict[str, str]  # path under sources/ -> sha, regular files of the import folders
    dirs: set[str]
    foreign: set[str]  # not ours to copy: symlinks, special files, entries not named like an import id


def _source_tree(lib: Library) -> _Tree:
    files: dict[str, str] = {}
    dirs: set[str] = set()
    foreign: set[str] = set()
    for entry in sorted(lib.sources.iterdir()):
        if entry.is_symlink() or not entry.is_dir() or not _IMPORT_ID.match(entry.name):
            foreign.add(entry.name)
            continue
        dirs.add(entry.name)
        for folder, subdirs, names in os.walk(entry):
            for name in subdirs + names:
                path = Path(folder) / name
                rel = path.relative_to(lib.sources).as_posix()
                if path.is_symlink() or not (path.is_dir() or path.is_file()):
                    foreign.add(rel)
                elif path.is_dir():
                    dirs.add(rel)
                elif not name.endswith(PART):  # a .part is an interrupted copy
                    files[rel] = file_sha256(path)
    return _Tree(files, dirs, foreign)


def _prefixes(rel: str) -> list[str]:
    parts = rel.split("/")
    return ["/".join(parts[:n]) for n in range(1, len(parts))]


def _clashes(rel: str, other: _Tree) -> bool:
    """True when the other copy holds something at rel, or on the way to it, that is not a folder."""
    return rel in other.dirs or any(p in other.files or p in other.foreign for p in _prefixes(rel))


def compare(a: Library, b: Library) -> Plan:
    """Import folders are write-once: a file on one side only is copied, anything else that differs is a conflict."""
    ta, tb = _source_tree(a), _source_tree(b)
    conflicts = ta.foreign | tb.foreign | {rel for rel in ta.files.keys() & tb.files.keys() if ta.files[rel] != tb.files[rel]}

    def only(mine: _Tree, theirs: _Tree) -> dict[str, str]:
        todo = {rel: sha for rel, sha in sorted(mine.files.items()) if rel not in theirs.files and rel not in conflicts}
        clashing = {rel for rel in todo if _clashes(rel, theirs)}
        conflicts.update(clashing)
        return {rel: sha for rel, sha in todo.items() if rel not in clashing}

    return Plan(only(ta, tb), only(tb, ta), sorted(conflicts))


def _linked(dst: Library, rel: str) -> bool:
    """True when a folder on the way to rel is a symlink or a file: writing would leave sources/ or fail."""
    folder = dst.sources
    for part in rel.split("/")[:-1]:
        folder = folder / part
        if folder.is_symlink() or (os.path.lexists(folder) and not folder.is_dir()):
            return True
    return False


class Copied(NamedTuple):
    done: list[str]
    bad: list[tuple[str, str, str]]  # (side, path, expected sha): the source's bytes were not the expected ones
    conflicts: list[str]  # refused or failed, with the reason


def copy(src: Library, dst: Library, side: str, todo: dict[str, str]) -> Copied:
    done: list[str] = []
    bad: list[tuple[str, str, str]] = []
    conflicts: list[str] = []
    for rel, sha in todo.items():
        if _linked(dst, rel):  # planned before a link could appear: never write through one
            conflicts.append(rel)
            continue
        try:
            fsops.copy_verified(src.sources / rel, dst.sources / rel, sha)
        except fsops.HashMismatch:
            bad.append((side, f"sources/{rel}", sha))
        except OSError as e:  # e.g. a name the other filesystem cannot hold
            conflicts.append(f"{rel}: {e}")
        else:
            done.append(rel)
    return Copied(done, bad, conflicts)

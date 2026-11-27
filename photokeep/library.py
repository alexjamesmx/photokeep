import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from photokeep import events, locks

FORMAT_VERSION = 1
SUBDIRS = ("originals", "sources", "log", "trash", "cache", "app")
# Allowed to be missing: cache/ is safe to delete, and an app update cut off mid-swap leaves no app/
# until `sync --update-app` puts it back.
_MAY_BE_MISSING = ("cache", "app")


class LibraryError(Exception):
    pass


class CopyBusy(LibraryError):
    """Another process (a sync, the viewer) holds the copy's lock."""


@dataclass(frozen=True)
class Library:
    root: Path
    library_id: str
    copy_id: str
    copy_name: str

    @property
    def originals(self) -> Path:
        return self.root / "originals"

    @property
    def sources(self) -> Path:
        return self.root / "sources"

    @property
    def log_dir(self) -> Path:
        return self.root / "log"

    @property
    def trash(self) -> Path:
        return self.root / "trash"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def app(self) -> Path:
        return self.root / "app"

    @property
    def quarantine(self) -> Path:
        """Corrupt files repair replaced, one folder per run; created by repair, never synced or scanned."""
        return self.root / "quarantine"

    def log_path(self, copy_id: str | None = None) -> Path:
        return self.log_dir / f"{copy_id or self.copy_id}.jsonl"

    def cache_dir(self) -> Path:
        """cache/, recreated if it was deleted."""
        _check_folder(self.cache, may_be_missing=True)
        self.cache.mkdir(exist_ok=True)
        return self.cache

    def cache_db(self, name: str) -> Path:
        """cache/<name> for SQLite, refused when it or a journal beside it is a symlink: writes would follow it."""
        path = self.cache_dir() / name
        for p in (path, *(path.with_name(name + s) for s in ("-wal", "-shm", "-journal"))):
            if p.is_symlink():
                raise LibraryError(f"{p} is a symlink; a cache file must be a real file")
        return path


def _check_folder(path: Path, may_be_missing: bool = False) -> None:
    # A library folder that is a symlink would send writes and deletes wherever it points.
    if path.is_symlink():
        raise LibraryError(f"{path} is a symlink; a library folder must be a real folder")
    if not path.is_dir() and not (may_be_missing and not path.exists()):
        raise LibraryError(f"{path} is not a folder")


def _check_folders(root: Path) -> None:
    for name in SUBDIRS:
        _check_folder(root / name, may_be_missing=name in _MAY_BE_MISSING)


def _write_json(path: Path, data: dict) -> None:
    # Temp file + rename so a crash never leaves a half-written file.
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _absolute(root: Path) -> Path:
    # Every path in a copy is built from its root, and some are joined to it twice (cache keys) or shown
    # to the owner, so a relative root would break the moment it is used. Not resolved: a copy reached
    # through a symlinked mount point keeps the path it was given.
    return Path(root).absolute()


def _prepare(root: Path) -> None:
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise LibraryError(f"{root} is not empty")
    root.mkdir(parents=True, exist_ok=True)
    for d in SUBDIRS:
        (root / d).mkdir()
    _check_folders(root)


def _create(root: Path, name: str, library_id: str, created: str | None = None) -> Library:
    root = _absolute(root)
    _prepare(root)
    now = events.utcnow()
    copy_id = uuid.uuid4().hex
    _write_json(root / "library.json", {"format": FORMAT_VERSION, "library_id": library_id, "created": created or now})
    _write_json(root / "copy.json", {"copy_id": copy_id, "name": name, "created": now})
    lib = Library(root, library_id, copy_id, name)
    events.append(lib, [events.new_event(lib, "copy.name", ts=now, name=name)])
    return lib


def init_library(root: Path, name: str) -> Library:
    return _create(root, name, uuid.uuid4().hex)


def init_copy(root: Path, name: str, of: Library) -> Library:
    created = json.loads((of.root / "library.json").read_text(encoding="utf-8"))["created"]
    return _create(root, name, of.library_id, created)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LibraryError(f"{path.parent} is not a Photokeep library (missing {path.name})") from None
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise LibraryError(f"{path} is unreadable: {e}") from None


def open_library(root: Path) -> Library:
    root = _absolute(root)
    info = _read_json(root / "library.json")
    if info.get("format", 0) > FORMAT_VERSION:
        raise LibraryError(
            f"{root} uses library format {info['format']}, newer than supported {FORMAT_VERSION}; update Photokeep"
        )
    copy = _read_json(root / "copy.json")
    _check_folders(root)
    try:
        return Library(root, info["library_id"], copy["copy_id"], copy["name"])
    except KeyError as e:
        raise LibraryError(f"{root} has an incomplete library.json or copy.json (missing {e})") from None


@contextmanager
def locked(lib: Library) -> Iterator[None]:
    """Hold the copy's exclusive lock for changing its files; fail at once if another process holds it."""
    fd = os.open(lib.cache_dir() / "lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        try:
            locks.flock(fd, wait=False)
        except BlockingIOError:
            raise CopyBusy(f"{lib.root} is busy (another sync or the viewer is changing files)") from None
        yield
    finally:
        os.close(fd)  # also releases the lock

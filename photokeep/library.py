import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from photokeep import events

try:
    import fcntl
except ImportError:  # Windows: no flock, so a second writer is not stopped (best effort)
    fcntl = None  # type: ignore[assignment]

FORMAT_VERSION = 1
SUBDIRS = ("originals", "sources", "log", "trash", "cache", "app")


class LibraryError(Exception):
    pass


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

    def log_path(self, copy_id: str | None = None) -> Path:
        return self.log_dir / f"{copy_id or self.copy_id}.jsonl"


def _write_json(path: Path, data: dict) -> None:
    # Temp file + rename so a crash never leaves a half-written file.
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _prepare(root: Path) -> None:
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise LibraryError(f"{root} is not empty")
    root.mkdir(parents=True, exist_ok=True)
    for d in SUBDIRS:
        (root / d).mkdir()


def _create(root: Path, name: str, library_id: str, created: str | None = None) -> Library:
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
    info = _read_json(root / "library.json")
    if info.get("format", 0) > FORMAT_VERSION:
        raise LibraryError(
            f"{root} uses library format {info['format']}, newer than supported {FORMAT_VERSION}; update Photokeep"
        )
    copy = _read_json(root / "copy.json")
    try:
        return Library(root, info["library_id"], copy["copy_id"], copy["name"])
    except KeyError as e:
        raise LibraryError(f"{root} has an incomplete library.json or copy.json (missing {e})") from None


@contextmanager
def locked(lib: Library) -> Iterator[None]:
    """Hold the copy's exclusive lock for changing its files; fail at once if another process holds it."""
    if fcntl is None:
        yield
        return
    lib.cache.mkdir(exist_ok=True)  # cache/ is safe to delete, so it may be gone
    fd = os.open(lib.cache / "lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LibraryError(f"{lib.root} is busy (another sync or the viewer is changing files)") from None
        yield
    finally:
        os.close(fd)  # also releases the lock

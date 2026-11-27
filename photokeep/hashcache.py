import hashlib
import sqlite3
from pathlib import Path

from photokeep.library import Library


def file_sha256(path: Path) -> str:
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


class HashCache:
    """SHA-256 of library files, trusted while a file's size and mtime are unchanged."""

    def __init__(self, lib: Library):
        self.root = lib.root
        lib.cache.mkdir(exist_ok=True)  # cache/ is safe to delete, so it may be gone
        # Autocommit keeps each write lock short, so a long scan never blocks another process;
        # WAL + NORMAL makes those commits cheap. A cache may lose its last rows on power loss.
        self.db = sqlite3.connect(lib.cache / "files.sqlite", isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS files"
                        "(path TEXT PRIMARY KEY, size INT, mtime_ns INT, sha256 TEXT)")

    def __enter__(self) -> "HashCache":
        return self

    def __exit__(self, *exc) -> None:
        self.db.close()

    def _key(self, path: Path) -> str:
        return (self.root / path).relative_to(self.root).as_posix()

    def sha(self, path: Path, rehash: bool = False) -> str:
        full = self.root / path
        key, st = self._key(full), full.stat()
        if not rehash:
            row = self.db.execute("SELECT sha256 FROM files WHERE path = ? AND size = ? AND mtime_ns = ?",
                                  (key, st.st_size, st.st_mtime_ns)).fetchone()
            if row:
                return row[0]
        # Stat before hashing: a change during the read leaves a stat that no longer matches.
        digest = file_sha256(full)
        self.db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?)",
                        (key, st.st_size, st.st_mtime_ns, digest))
        return digest

    def record(self, path: Path, sha: str) -> None:
        """Store a hash the caller has just verified, so the file is not read again to learn it."""
        st = (self.root / path).stat()
        self.db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?)",
                        (self._key(path), st.st_size, st.st_mtime_ns, sha))

    def forget(self, path: Path) -> None:
        self.db.execute("DELETE FROM files WHERE path = ?", (self._key(path),))

    def move(self, src: Path, dst: Path) -> None:
        # A rename keeps size and mtime, so the row stays valid at the new path.
        self.forget(dst)
        self.db.execute("UPDATE files SET path = ? WHERE path = ?", (self._key(dst), self._key(src)))

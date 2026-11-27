"""Check a copy's files against the folded state, and repair them from another copy."""
import os
import random
from dataclasses import dataclass
from datetime import UTC, datetime

from photokeep import fsops
from photokeep.events import read_all
from photokeep.fsops import expected_files, inside, scan
from photokeep.hashcache import HashCache
from photokeep.library import Library, LibraryError, locked
from photokeep.state import fold

_STAMP = "%Y%m%dT%H%M%SZ"


@dataclass
class VerifyReport:
    checked: int  # files hashed
    missing: list[str]
    corrupt: list[str]
    untracked: list[str]

    @property
    def ok(self) -> bool:
        return not (self.missing or self.corrupt or self.untracked)

    def to_json(self) -> dict:
        return {"ok": self.ok, "checked": self.checked, "missing": self.missing,
                "corrupt": self.corrupt, "untracked": self.untracked}


def _is_file(lib: Library, rel: str) -> bool:
    path = lib.root / rel
    return inside(lib, path) and path.is_file() and not path.is_symlink()


def verify(lib: Library, sample: int | None = None, rng: random.Random | None = None) -> VerifyReport:
    """Rehash every expected file, or a random `sample` of them; writes only the hash cache."""
    state = fold(read_all(lib))
    expected = expected_files(state)
    present = [(rel, s) for rel, s in expected if _is_file(lib, rel)]
    missing = sorted(rel for rel, _ in expected if not _is_file(lib, rel))
    if sample is not None:
        present = (rng or random.Random()).sample(present, min(sample, len(present)))
    corrupt = []
    with HashCache(lib) as cache:
        for rel, s in sorted(present):
            try:
                if cache.sha(lib.root / rel, rehash=True) != s:
                    corrupt.append(rel)
            except FileNotFoundError:
                missing.append(rel)
    # A bad file at an expected path is reported as corrupt, not also as untracked.
    paths = {rel for rel, _ in expected}
    known = {f["sha256"] for i in state.items.values() for f in i.files}
    found = scan(lib, cleanup=False)
    untracked = sorted(p for s, ps in found.items() if s not in known for p in ps if p not in paths)
    return VerifyReport(len(present), sorted(missing), corrupt, untracked)


def _quarantine(lib: Library, rels: list[str]) -> None:
    stamp = datetime.now(UTC).strftime(_STAMP)
    with HashCache(lib) as cache:
        for rel in rels:
            target = lib.cache_dir() / "quarantine" / stamp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(lib.root / rel, target)
            cache.forget(lib.root / rel)


def repair(lib: Library, other: Library) -> VerifyReport:
    """Quarantine corrupt files, then restore missing and corrupt ones from `other`; return a fresh verify."""
    if lib.library_id != other.library_id:
        raise LibraryError(f"{lib.root} and {other.root} are copies of different libraries")
    if lib.copy_id == other.copy_id:
        raise LibraryError(f"{lib.root} and {other.root} are the same copy")
    with locked(lib), locked(other):
        report = verify(lib)
        _quarantine(lib, report.corrupt)
        want = {rel: s for rel, s in expected_files(fold(read_all(lib)))}
        held = scan(other, cleanup=False)
        with HashCache(lib) as cache:
            for rel in report.missing + report.corrupt:
                sources = held.get(want[rel])
                target = lib.root / rel
                if not sources or not inside(lib, target):
                    continue
                try:
                    fsops.copy_verified(other.root / sources[0], target, want[rel])
                except fsops.HashMismatch:  # the other copy's file is rotten too
                    continue
                cache.record(target, want[rel])
        return verify(lib)

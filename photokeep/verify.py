"""Check a copy's files against the folded state, and repair them from another copy."""
import os
import random
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

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


@dataclass
class RepairReport:
    restore: list[str]  # bad files the other copy holds good bytes for: restored, or with dry_run to restore
    no_source: list[str]  # bad files the other copy holds no good bytes for: left as they are
    failed: fsops.Failed  # could not be restored this run; a corrupt one stays in place
    quarantine: str | None  # this run's folder of replaced corrupt files, relative to the root
    after: VerifyReport  # verify after the repair; with dry_run, the one the plan was made from
    dry_run: bool

    @property
    def ok(self) -> bool:
        """The copy is whole after the repair; with dry_run, it would be, as far as the other copy's cache knows."""
        return not (self.no_source or self.after.untracked) if self.dry_run else self.after.ok

    def to_json(self) -> dict:
        return self.after.to_json() | {
            "ok": self.ok, "dry_run": self.dry_run, "restore": self.restore, "no_source": self.no_source,
            "failed": [{"path": path, "reason": reason} for path, reason in self.failed], "quarantine": self.quarantine}


def _run_folder(lib: Library) -> Path:
    """A new folder under quarantine/ for this run: outside cache/, which is safe to delete."""
    if lib.quarantine.is_symlink():  # moving corrupt files through it would send them elsewhere
        raise LibraryError(f"{lib.quarantine} is a symlink; quarantine/ must be a real folder")
    lib.quarantine.mkdir(exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=datetime.now(UTC).strftime(_STAMP) + "-", dir=lib.quarantine))


class _Restorer:
    """Puts good bytes from another copy in place of a copy's missing and corrupt files."""

    def __init__(self, lib: Library, other: Library, held: dict[str, list[str]]):
        self.lib, self.other, self.held = lib, other, held
        self.quarantine: Path | None = None  # made when the first corrupt file is moved aside

    def restore(self, rel: str, sha: str, corrupt: bool) -> None:
        target, source = self.lib.root / rel, self.other.root / self.held[sha][0]
        if not inside(self.lib, target):
            raise fsops.CopyError(f"{rel} leads outside originals/ and trash/")
        if not corrupt:
            return fsops.copy_verified(source, target, sha)
        # The replacement is staged and checked first: when the other copy's bytes turn out bad too,
        # the corrupt file is never moved, and the copy keeps the only bytes it has.
        part = fsops.stage_verified(source, target, sha)
        try:
            self.quarantine = self.quarantine or _run_folder(self.lib)
            aside = self.quarantine / rel
            aside.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, aside)
            os.replace(part, target)
        finally:
            part.unlink(missing_ok=True)


@contextmanager
def _both_locked(lib: Library, other: Library) -> Iterator[None]:
    with locked(lib), locked(other):
        yield


def repair(lib: Library, other: Library, dry_run: bool = False) -> RepairReport:
    """Restore lib's missing and corrupt files from good copies in other; with dry_run, only plan it.

    A corrupt file is replaced only by verified bytes and is kept under quarantine/; with no good
    copy in other it stays where it is.
    """
    if lib.library_id != other.library_id:
        raise LibraryError(f"{lib.root} and {other.root} are copies of different libraries")
    if lib.copy_id == other.copy_id:
        raise LibraryError(f"{lib.root} and {other.root} are the same copy")
    with nullcontext() if dry_run else _both_locked(lib, other):
        report = verify(lib)
        want = dict(expected_files(fold(read_all(lib))))  # path -> sha
        held = scan(other, cleanup=False)
        bad = sorted(report.missing + report.corrupt)
        todo, no_source = [rel for rel in bad if want[rel] in held], [rel for rel in bad if want[rel] not in held]
        if dry_run:
            return RepairReport(todo, no_source, [], None, report, dry_run=True)
        restorer, done, failed = _Restorer(lib, other, held), [], []
        with HashCache(lib) as cache:
            for rel in todo:
                try:
                    restorer.restore(rel, want[rel], corrupt=rel in report.corrupt)
                except (fsops.CopyError, OSError) as e:  # a HashMismatch too: the other copy's file is rotten
                    failed.append((rel, fsops.failure(e)))
                else:
                    cache.record(lib.root / rel, want[rel])
                    done.append(rel)
        folder = restorer.quarantine.relative_to(lib.root).as_posix() if restorer.quarantine else None
        return RepairReport(done, no_source, failed, folder, verify(lib), dry_run=False)

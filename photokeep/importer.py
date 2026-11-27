"""The shared import pipeline (spec 6.6): an adapter lists candidate files and the provider files to keep; this
stages and hashes them, reads their EXIF, skips what the library holds or deleted, and adds the rest.

Files go into place first and the log is written last, in one append, so a run that was cut off is finished by
running it again: it reuses the import id, the staged files and the sources/ folder the first run left.
"""
import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PureWindowsPath
from typing import BinaryIO

from photokeep import __version__, events, fsops
from photokeep.cache import Cache, regular
from photokeep.hashcache import HashCache
from photokeep.library import Library, locked
from photokeep.metadata import derive, fingerprint
from photokeep.paths import assign, folder_of
from photokeep.staging import UNREADABLE, ImportFailed, Say, Stage, durable, exiftool, import_id_for, \
    keep_sources, listing, problem, ticking, why
from photokeep.state import State, fold

_ADAPTER = re.compile(r"[a-z0-9]+")  # the middle of an import id
_OWN = frozenset({"exif.json", "manifest.json"})  # Photokeep's files in sources/<import id>/


@dataclass(frozen=True)
class Candidate:
    """A media file an adapter found."""
    source_path: str  # relative POSIX path inside the source
    read: Callable[[], BinaryIO]
    size: int
    mtime: str | None  # UTC ISO, as the source gives it
    name: str  # the original name the item keeps
    provider: dict  # metadata.provider_record of what the provider says about it, or {}
    albums: tuple[str, ...] = ()
    trashed: bool = False  # in the provider's trash: imported, then trashed
    favorite: bool = False
    live_of: str | None = None  # source_path of the photo this clip belongs to
    edit_of: str | None = None  # source_path of the original


@dataclass(frozen=True)
class SourceFile:
    """A provider's metadata file, kept verbatim as sources/<import id>/<path>."""
    path: str
    read: Callable[[], bytes]


@dataclass
class _New:
    """An item the import adds, with what its copies in the source say."""
    main: Candidate
    sha: str
    folder: str
    fingerprint: list | None
    version_of: str | None
    albums: set[str]
    favorite: bool
    trashed: bool  # every copy is in the provider's trash
    clip: tuple[Candidate, str] | None  # the Live clip and its SHA-256
    edit_of: str | None = None

    def files(self) -> list[tuple[str, Candidate, str]]:
        """(role, candidate, SHA-256) of each file, the main one first."""
        return [("main", self.main, self.sha), *([("live", *self.clip)] if self.clip else [])]


def _usable(path: str) -> bool:
    """A relative path that stays inside the folder it is joined to, on any system."""
    return (not PureWindowsPath(path).drive and not {"\\", "\0"} & set(path)
            and all(part not in ("", ".", "..") for part in path.split("/")))


def _check_listing(candidates: list[Candidate], sources: list[SourceFile]) -> None:
    # Source paths become paths on disk; an adapter reads them from third-party archives.
    for paths, own in (([c.source_path for c in candidates], frozenset()), ([s.path for s in sources], _OWN)):
        seen: set[str] = set()
        for path in paths:
            if not _usable(path) or path in own:
                raise ImportFailed(f"unusable source path {path!r}")
            if path in seen:
                raise ImportFailed(f"{path} is listed twice")
            seen.add(path)


def refuse_overlap(lib: Library, sources: Iterable[Path]) -> None:
    """The copy is written to and the sources only read, so neither may lie inside the other."""
    for source in sources:
        if fsops.overlap(lib.root, source):
            raise ImportFailed(f"{lib.root} and {source} overlap: the copy must lie outside every source")


def _tag(candidates: list[Candidate]) -> str:
    """The same listing gives the same tag, so a run after a cut-off one finds its import id."""
    return hashlib.sha256(json.dumps(listing(candidates)).encode()).hexdigest()[:6]


def _read_kept(sources: list[SourceFile], problems: list[dict]) -> dict[str, bytes]:
    kept = {}
    for s in sources:
        try:
            kept[s.path] = s.read()
        except UNREADABLE as e:
            problems.append({"source_path": s.path, "reason": f"could not be read: {why(e)}"})
    return kept


def _version_of(state: State, fp: tuple | None) -> str | None:
    """The first live item with this fingerprint: the candidate is another version of it (spec 6.3)."""
    same = [state.items[i] for i in state.fingerprints.get(fp, ())] if fp else []
    live = [i for i in same if i.status == "live"]
    return min(live, key=lambda i: (i.first_ts, i.id)).id if live else None


def _plan(state: State, candidates: list[Candidate], hashed: dict[str, str], exif: dict[str, dict],
          problems: list[dict]) -> tuple[list[_New], Counter]:
    """The items to add in source order, and how many candidates each kind of skip took."""
    by_path = {c.source_path: c for c in candidates}
    clips: dict[str, Candidate] = {}
    for c in candidates:
        if c.live_of in by_path and c.live_of != c.source_path:
            clips.setdefault(c.live_of, c)
    attached = {c.source_path for c in clips.values()}
    live = {f["sha256"] for i in state.items.values() if i.status == "live" for f in i.files}
    gone = {f["sha256"] for i in state.items.values() if i.blocked for f in i.files}

    def skip(sha: str, fp: tuple | None) -> str | None:
        known = state.items.get(sha)
        # A known item is judged by its own state, as fold does: versions share fingerprints.
        if known.blocked if known else sha in gone or state.is_blocked(sha, fp):
            return "skipped_deleted"
        return "already_in_library" if sha in live else None

    new: dict[str, _New] = {}
    skipped: Counter = Counter()
    for c in candidates:
        if c.source_path in attached:
            continue
        found = clips.get(c.source_path)
        clip = (found, hashed[found.source_path]) if found and found.source_path in hashed else None
        sha = hashed.get(c.source_path)
        if sha is None:
            if clip:
                problems.append(problem(clip[0], f"its photo {c.source_path} could not be read"))
            continue
        meta = derive(exif.get(c.source_path, {}), c.provider, c.name, c.mtime)
        fp = fingerprint(meta)
        if verdict := skip(sha, fp):
            skipped[verdict] += 1 + bool(clip)  # a clip goes where its photo goes
            continue
        if clip and skip(clip[1], None) == "skipped_deleted":
            skipped["skipped_deleted"] += 1
            clip = None
        if it := new.get(sha):
            skipped["duplicates_in_source"] += 1
            it.albums.update(c.albums)
            it.favorite, it.trashed = it.favorite or c.favorite, it.trashed and c.trashed
            if clip and it.clip is None:  # a clip only this copy has is not lost
                it.clip = clip
            elif clip:
                skipped["duplicates_in_source"] += 1
            continue
        new[sha] = _New(c, sha, folder_of(meta["taken"]), list(fp) if fp else None, _version_of(state, fp),
                        set(c.albums), c.favorite, c.trashed, clip)
    for it in new.values():
        original = it.main.edit_of
        target = hashed.get(original) if original else None
        if original is None or target == it.sha:
            continue
        if target and (target in new or target in state.items and state.items[target].status == "live"):
            it.edit_of = target
        else:
            problems.append(problem(it.main, f"its original {original} was not imported: kept without the link"))
    return list(new.values()), skipped


def _events(lib: Library, import_id: str, adapter: str, new: list[_New], start: datetime) -> list[dict]:
    """Every import first, then the marks: a trash folded before a later import of the same fingerprint would
    make fold ignore that import. Each event a microsecond after the last keeps their order."""
    plain: list[tuple[str, dict]] = []
    for it in new:
        files = [{"role": role, "name": c.name, "sha256": sha, "size": c.size, "mtime": c.mtime}
                 for role, c, sha in it.files()]
        links = {k: v for k, v in (("version_of", it.version_of), ("edit_of", it.edit_of)) if v}
        plain.append(("import", {"item": it.sha, "import": import_id, "folder": it.folder, "files": files,
                                 "fingerprint": it.fingerprint, **links}))
    for it in new:
        plain += [("album.add", {"item": it.sha, "kind": "album", "album": title}) for title in sorted(it.albums)]
        plain += [("favorite", {"item": it.sha})] if it.favorite else []
        plain += [("trash", {"item": it.sha, "reason": f"Trashed in {adapter}"})] if it.trashed else []
    return [events.new_event(lib, op, ts=events.ts_str(start + timedelta(microseconds=k)), **fields)
            for k, (op, fields) in enumerate(plain)]


def _jobs(lib: Library, new: list[_New], logged: list[dict]) -> list[tuple[Candidate, str, str]]:
    """(candidate, SHA-256, library path) of every file the import puts in place, each path free or already
    holding those bytes (put there by a cut-off run): nothing is ever overwritten."""
    paths = assign(fold(events.ordered([*events.read_all(lib), *logged])))
    if lost := [it.main.source_path for it in new if it.sha not in paths]:
        raise ImportFailed(f"an event in the log dated after this run blocks importing {lost[0]}")
    jobs = [(c, sha, paths[it.sha][role]) for it in new for role, c, sha in it.files()]
    with HashCache(lib) as hashes:

        def free(dst: Path, sha: str) -> bool:
            return fsops.inside(lib, dst) and (not os.path.lexists(dst) or regular(dst) and hashes.sha(dst) == sha)

        taken = [rel for _, sha, rel in jobs if not free(lib.root / rel, sha)]
    if taken:
        raise ImportFailed(f"{len(taken)} path(s) this import needs hold files the log does not know, first "
                           f"{taken[0]}: see photokeep verify {lib.root}")
    return jobs


def _place(lib: Library, stage: Stage, jobs: list[tuple[Candidate, str, str]], say: Say) -> None:
    with HashCache(lib) as hashes:
        for c, sha, rel in ticking(jobs, len(jobs), say, "moving into the library"):
            dst = lib.root / rel
            if regular(dst) and hashes.sha(dst) == sha:
                continue  # moved by a cut-off run
            stage.place(c.source_path, c.read, c.size, sha, dst)
            hashes.record(dst, sha)


def _manifest(import_id: str, adapter: str, candidates: list[Candidate], hashed: dict[str, str],
              jobs: list[tuple[Candidate, str, str]]) -> dict:
    """Every candidate with the library path its bytes got here (None: not imported by this import)."""
    placed = {sha: rel for _, sha, rel in jobs}
    files = []
    for c in candidates:
        sha = hashed.get(c.source_path)
        files.append({"source_path": c.source_path, "sha256": sha, "size": c.size, "mtime": c.mtime,
                      "path": placed.get(sha) if sha else None})
    # exif_root: the folder exiftool ran on, which every SourceFile in exif.json starts with
    return {"import": import_id, "adapter": adapter, "tool": f"photokeep {__version__} import",
            "exif_root": import_id, "files": files}


def _fill(folder: Path, kept: dict[str, bytes], raw_exif: bytes) -> None:
    for path, data in kept.items():
        durable(folder / path, data)
    durable(folder / "exif.json", raw_exif)


def _summary(new: list[_New], skipped: Counter, problems: list[dict]) -> dict:
    return {"imported": len(new), "versions": sum(bool(it.version_of) for it in new),
            "edits": sum(bool(it.edit_of) for it in new), "live_clips": sum(bool(it.clip) for it in new),
            "already_in_library": skipped["already_in_library"],
            "duplicates_in_source": skipped["duplicates_in_source"], "skipped_deleted": skipped["skipped_deleted"],
            "trashed": sum(it.trashed for it in new), "album_links": sum(len(it.albums) for it in new),
            "favorites": sum(it.favorite for it in new),
            "bytes": sum(c.size for it in new for _, c, _ in it.files()), "problems": problems}


def run(lib: Library, adapter: str, candidates: list[Candidate], sources: list[SourceFile], dry_run: bool,
        now: datetime, progress: Say | None = None) -> dict:
    """Import the candidates into lib and keep the sources verbatim; with dry_run, only count what would happen.

    Staging and exiftool run in a dry run too: cache/ is rebuildable, and the dry run removes the staging it made.
    """
    say = progress or (lambda _: None)
    if not _ADAPTER.fullmatch(adapter):
        raise ValueError(f"adapter {adapter!r} is not lower-case letters and digits")
    _check_listing(candidates, sources)
    tool = exiftool()
    problems: list[dict] = []
    kept = _read_kept(sources, problems)
    with locked(lib):
        state = fold(events.read_all(lib))
        stage = Stage(lib, import_id_for(lib, adapter, _tag(candidates), now))
        hashed, raw_exif, exif = stage.hashed(lib, candidates, tool, problems, say)
        new, skipped = _plan(state, candidates, hashed, exif, problems)
        if new:
            logged = _events(lib, stage.id, adapter, new, now)
            jobs = _jobs(lib, new, logged)
            if not dry_run:
                keep_sources(lib, stage.id, _manifest(stage.id, adapter, candidates, hashed, jobs),
                             lambda folder: _fill(folder, kept, raw_exif))
                _place(lib, stage, jobs, say)
                events.append(lib, logged)
                say("refreshing the viewer index")
                Cache(lib).refresh(render=True)
        if not dry_run or stage.created:
            stage.remove()
    return _summary(new, skipped, problems)

"""The shared import pipeline (spec 6.6): an adapter lists candidate files and the provider files to keep; this
stages and hashes them, reads their EXIF, skips what the library deleted, records a second source for what it
holds, and adds the rest.

Files go into place first and the log is written last, in one append, so a run that was cut off is finished by
running it again. A file that cannot be read is left for a later run, under a new import id: an import folder
only records files whose fate is settled.
"""
import hashlib
import json
import os
import re
import shutil
import zipfile
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from photokeep import __version__, events, fsops
from photokeep.cache import Cache, regular
from photokeep.hashcache import HashCache
from photokeep.importplan import Candidate, Planned, SourceFile, Touched, clips_of, events_of, plan, summary
from photokeep.library import Library, locked
from photokeep.paths import assign, utf8
from photokeep.progress import Say, ticking
from photokeep.sources import durable, find_folder, read_folder, write_folder
from photokeep.staging import ImportFailed, Stage, exiftool, staged_id
from photokeep.state import fold

_ADAPTER = re.compile(r"[a-z0-9]+")  # the middle of an import id
_OWN = frozenset({"exif.json", "manifest.json"})  # Photokeep's files in sources/<import id>/
_UNREADABLE = (OSError, EOFError, zipfile.BadZipFile, zlib.error)  # a file gone, a damaged zip member
_IN_LIBRARY = frozenset({"imported", "live_clips", "already_in_library", "duplicates_in_source"})
_SPARE = 1 << 30  # free space staging leaves on the copy's disk: thumbnails, the log and the system need some too


@dataclass
class _Batch:
    """The candidates one import id records: an earlier run wrote its sources/ folder, or this run staged them."""
    stage: Stage
    candidates: list[Candidate]
    shas: dict[str, str]
    raw_exif: bytes
    exif: dict[str, dict]


def _why(e: BaseException) -> str:
    return fsops.failure(e) if isinstance(e, OSError) else str(e)  # a full disk stops the run


def _usable(path: str) -> bool:
    """A relative path that stays inside the folder it is joined to, on any system, and fits on one line of an
    exiftool argument file."""
    return (not PureWindowsPath(path).drive and not {"\\", "\0", "\n", "\r"} & set(path)
            and all(part not in ("", ".", "..") for part in path.split("/")))


def _check_listing(candidates: list[Candidate], sources: list[SourceFile]) -> None:
    # Source paths become paths on disk; an adapter reads them from third-party archives. Paths, names and albums
    # go into the log and manifests, which are UTF-8: one that is not would stop the run once its files were moved.
    for paths, own in (([c.source_path for c in candidates], frozenset()), ([s.path for s in sources], _OWN)):
        seen: set[str] = set()
        for path in paths:
            if not _usable(path) or path in own:
                raise ImportFailed(f"unusable source path {path!r}")
            if not utf8(path):
                raise ImportFailed(f"source path {path!r} is not UTF-8: rename the file")
            if path in seen:
                raise ImportFailed(f"{path} is listed twice")
            seen.add(path)
    for c in candidates:
        if not all(map(utf8, (c.name, *c.albums))):
            raise ImportFailed(f"{c.source_path}: the name or an album the source gives it is not UTF-8")


def _refuse_overlap(lib: Library, roots: Iterable[Path]) -> None:
    """The copy is written to and the sources only read, so neither may lie inside the other."""
    for root in roots:
        if fsops.overlap(lib.root, root):
            raise ImportFailed(f"{lib.root} and {root} overlap: the copy must lie outside every source")


def _tag(candidates: list[Candidate]) -> str:
    """The same listing gives the same tag, so a later run finds the import folder an earlier one wrote."""
    listing = sorted((c.source_path, c.size) for c in candidates)
    return hashlib.sha256(json.dumps(listing).encode()).hexdigest()[:6]


def _read_kept(sources: list[SourceFile]) -> dict[str, bytes]:
    """Every metadata file's bytes. Adapters read them to list the candidates, so one that fails now means the
    source changed meanwhile: the run stops before it writes anything."""
    kept = {}
    for s in sources:
        try:
            kept[s.path] = s.read()
        except _UNREADABLE as e:
            raise ImportFailed(f"{s.path} could not be read ({_why(e)}); nothing was imported") from None
    return kept


def _earlier(lib: Library, adapter: str, candidates: list[Candidate],
             open_stage: Callable[[str], Stage]) -> tuple[list[_Batch], list[Candidate]]:
    """The batches earlier runs of this source recorded in sources/, each found by the tag of what was left
    before it, and the candidates none of them records."""
    batches: list[_Batch] = []
    rest = candidates
    while rest and (folder := find_folder(lib, adapter, _tag(rest))):
        try:
            files, raw, exif = read_folder(folder)
        except (OSError, ValueError, TypeError, KeyError) as e:
            raise ImportFailed(f"{folder} cannot be read ({type(e).__name__}: {e})") from None
        mine = [c for c in rest if c.source_path in files]
        if not mine or len(mine) != len(files) or any(files[c.source_path][1] != c.size for c in mine):
            raise ImportFailed(f"{folder} lists other files than this source")
        shas = {c.source_path: files[c.source_path][0] for c in mine}
        batches.append(_Batch(open_stage(folder.name), mine, shas, raw, exif))
        rest = [c for c in rest if c.source_path not in files]
    return batches, rest


def _check_space(stage: Stage, rest: list[Candidate]) -> None:
    """Staging copies every file onto the copy's disk before one is moved into place: refuse to start a run that
    would fill it. Files a cut-off run staged whole are there already."""
    needed = sum(c.size for c in rest if stage.staged.get(c.source_path, (None, None))[1] != c.size)
    free = shutil.disk_usage(stage.top).free
    if free < needed + _SPARE:
        raise ImportFailed(f"staging in {stage.top} needs {needed / 1e9:.1f} GB and 1 GiB to spare, but only "
                           f"{free / 1e9:.1f} GB is free")


def _stage_rest(stage: Stage, rest: list[Candidate], tool: str, unread: dict[str, str], say: Say) -> _Batch:
    """Stage what no earlier run recorded and read its EXIF. A file that cannot be read goes in unread, with why:
    this batch leaves it out, for a later run to import."""
    _check_space(stage, rest)
    shas = {}
    for c in ticking(rest, len(rest), say, "staging"):
        sha = stage.intact(c.source_path, c.size)
        if sha is None:
            try:
                sha, size = stage.put(c.source_path, c.read, c.mtime)
            except _UNREADABLE as e:
                unread[c.source_path] = f"could not be read: {_why(e)}"
                continue
            if size != c.size:
                unread[c.source_path] = f"{size} bytes read, {c.size} listed"
                continue
        shas[c.source_path] = sha
    read = [c for c in rest if c.source_path in shas]
    say("reading EXIF with exiftool")
    raw, exif = stage.exif(tool, [c.source_path for c in read])
    return _Batch(stage, read, shas, raw, exif)


_Job = tuple[Candidate, str, str, Stage]  # candidate, SHA-256, library path, its staging


def _jobs(lib: Library, items: list[Planned], paths: dict[str, dict[str, str]],
          stage_of: dict[str, Stage]) -> list[_Job]:
    """Every file of a new item, with its library path: free, or already holding those bytes (put there by a
    cut-off run). Nothing is ever overwritten."""
    new = [it for it in items if not it.known]
    if lost := [it.main.source_path for it in new if it.sha not in paths]:
        raise ImportFailed(f"an event in the log dated after this run blocks importing {lost[0]}")
    jobs = [(c, sha, paths[it.sha][role], stage_of[c.source_path]) for it in new for role, c, sha in it.files()]
    with HashCache(lib) as hashes:

        def free(dst: Path, sha: str) -> bool:
            return fsops.inside(lib, dst) and (not os.path.lexists(dst) or regular(dst) and hashes.sha(dst) == sha)

        taken = [rel for _, sha, rel, _ in jobs if not free(lib.root / rel, sha)]
    if taken:
        raise ImportFailed(f"{len(taken)} path(s) this import needs hold files the log does not know, first "
                           f"{taken[0]}: see photokeep verify {lib.root}")
    return jobs


def _place(lib: Library, jobs: list[_Job], say: Say) -> None:
    with HashCache(lib) as hashes:
        for c, sha, rel, stage in ticking(jobs, len(jobs), say, "moving into the library"):
            dst = lib.root / rel
            if regular(dst) and hashes.sha(dst) == sha:
                continue  # moved by a cut-off run
            try:
                stage.place(c.source_path, c.read, c.size, c.mtime, sha, dst)
            except (EOFError, zipfile.BadZipFile, zlib.error) as e:  # staged again from a source that now fails
                raise ImportFailed(f"{c.source_path} could not be read again ({type(e).__name__}: {e}); run the "
                                   "import again once it reads") from None
            hashes.record(dst, sha)


def _flush(lib: Library, rels: list[str]) -> None:
    """Flush every folder a file went into, up to originals/ or trash/, before the log names the files: else a
    power cut could keep the log line and lose the rename on some file systems."""
    folders = {lib.root.joinpath(*parts[:n]) for parts in (PurePosixPath(rel).parts for rel in rels)
               for n in range(1, len(parts))}
    for folder in sorted(folders):
        fsops.fsync_dir(folder)


def _manifest(batch: _Batch, adapter: str, outcome: dict[str, str], where: dict[str, str]) -> dict:
    """Every candidate the batch records, with the library path of its bytes (None: the library has none)."""
    files = [{"source_path": c.source_path, "sha256": batch.shas[c.source_path], "size": c.size, "mtime": c.mtime,
              "path": where.get(batch.shas[c.source_path]) if outcome.get(c.source_path) in _IN_LIBRARY else None}
             for c in batch.candidates]
    # exif_root: the folder exiftool's paths start with, which every SourceFile in exif.json begins with
    return {"import": batch.stage.id, "adapter": adapter, "tool": f"photokeep {__version__} import",
            "exif_root": batch.stage.id, "files": files}


def _fill(folder: Path, kept: dict[str, bytes], raw_exif: bytes) -> None:
    for path, data in kept.items():
        durable(folder / path, data)
    durable(folder / "exif.json", raw_exif)


def run(lib: Library, adapter: str, candidates: list[Candidate], sources: list[SourceFile], dry_run: bool,
        now: datetime, progress: Say | None = None, *, roots: Iterable[Path]) -> dict:
    """Import the candidates into lib and keep the sources verbatim; with dry_run, only count what would happen.
    roots are the source's files and folders on disk, which the copy must not overlap.

    Staging and exiftool run in a dry run too: cache/ is rebuildable, and the dry run removes the staging it made.
    """
    say = progress or (lambda _: None)
    if not _ADAPTER.fullmatch(adapter):
        raise ValueError(f"adapter {adapter!r} is not lower-case letters and digits")
    _check_listing(candidates, sources)
    _refuse_overlap(lib, roots)
    tool = exiftool()
    kept, clips = _read_kept(sources), clips_of(candidates)
    stages: list[Stage] = []

    def open_stage(import_id: str) -> Stage:
        stages.append(Stage(lib, import_id))
        return stages[-1]

    with locked(lib):
        try:
            log = events.read_all(lib)
            state = fold(log)
            problems: list[dict] = []
            unread: dict[str, str] = {}
            batches, rest = _earlier(lib, adapter, candidates, open_stage)
            current = _stage_rest(open_stage(staged_id(lib, adapter, _tag(rest), now)), rest, tool, unread,
                                  say) if rest else None
            batches += [current] if current else []
            shas = {p: sha for b in batches for p, sha in b.shas.items()}
            exif = {p: record for b in batches for p, record in b.exif.items()}
            import_of = {c.source_path: b.stage.id for b in batches for c in b.candidates}
            items, outcome = plan(state, candidates, shas, exif, import_of, clips, unread, problems)
            logged = events_of(lib, items, adapter, now, Touched.of(log))
            after = fold(events.ordered([*log, *logged]))
            paths = assign(after)
            jobs = _jobs(lib, items, paths, {c.source_path: b.stage for b in batches for c in b.candidates})
            if not dry_run:
                # Provider metadata is kept whenever a candidate is not one the library deleted.
                if current and any(outcome.get(c.source_path) != "skipped_deleted" for c in current.candidates):
                    where = {f["sha256"]: paths[i][f["role"]] for i in paths for f in after.items[i].files}
                    raw = current.raw_exif
                    write_folder(lib, current.stage.id, _manifest(current, adapter, outcome, where),
                                 lambda folder: _fill(folder, kept, raw))
                _place(lib, jobs, say)
                _flush(lib, [rel for _, _, rel, _ in jobs])
                events.append(lib, logged)
                say("refreshing the viewer index")
                Cache(lib).refresh(render=True)
                for stage in stages:
                    stage.remove()
        finally:
            if dry_run:
                for stage in stages:
                    if stage.created:
                        stage.remove()
    return summary(items, outcome, logged, problems)

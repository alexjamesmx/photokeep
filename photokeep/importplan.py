"""What an import does, decided without touching disk: from the folded state and each candidate's SHA-256 and
EXIF, the items it adds, the live ones it records another source of, what it skips, and the events that say so.
"""
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import BinaryIO

from photokeep import events
from photokeep.library import Library
from photokeep.metadata import derive, fingerprint
from photokeep.paths import folder_of
from photokeep.state import Item, State


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
    trashed: bool = False  # in the provider's trash: a new item is imported, then trashed
    favorite: bool = False
    live_of: str | None = None  # source_path of the photo this clip belongs to
    edit_of: str | None = None  # source_path of the original


@dataclass(frozen=True)
class SourceFile:
    """A provider's metadata file, kept verbatim as sources/<import id>/<path>."""
    path: str
    read: Callable[[], bytes]


@dataclass
class Planned:
    """An item the import adds, or a live one the library holds (known), of which this is another source."""
    main: Candidate
    sha: str
    import_id: str
    folder: str
    fingerprint: list | None
    known: Item | None
    albums: set[str]
    favorite: bool
    trashed: bool  # every copy is in the provider's trash; never for a known item
    clip: tuple[Candidate, str] | None = None  # the Live clip and its SHA-256
    version_of: str | None = None
    edit_of: str | None = None

    def files(self) -> list[tuple[str, Candidate, str]]:
        """(role, candidate, SHA-256) of each file, the main one first."""
        return [("main", self.main, self.sha), *([("live", *self.clip)] if self.clip else [])]


def problem(c: Candidate, reason: str) -> dict:
    return {"source_path": c.source_path, "reason": reason}


def clips_of(candidates: list[Candidate]) -> dict[str, Candidate]:
    """Each photo's Live clip by the photo's source path; a second clip claiming the same photo is a file of its own."""
    paths = {c.source_path for c in candidates}
    clips: dict[str, Candidate] = {}
    for c in candidates:
        if c.live_of in paths and c.live_of != c.source_path:
            clips.setdefault(c.live_of, c)
    return clips


def waiting(candidates: list[Candidate], shas: dict[str, str], clips: dict[str, Candidate]) -> dict[str, str]:
    """Readable candidates that wait for one that could not be read, by its path: a photo and its clip are one
    item and an edit links to its original, so a later run imports each pair together."""
    paths = {c.source_path for c in candidates}
    needs = ([(clip.source_path, photo) for photo, clip in clips.items()]
             + [(photo, clip.source_path) for photo, clip in clips.items()]
             + [(c.source_path, c.edit_of) for c in candidates if c.edit_of])
    root = {p: p for p in paths if p not in shas}
    grew = True
    while grew:
        grew = False
        for a, b in needs:
            if a in paths and a not in root and b in root:
                root[a], grew = root[b], True
    return {a: r for a, r in root.items() if a != r}


def _version_of(state: State, fp: tuple | None) -> str | None:
    """The first live item with this fingerprint: the candidate is another version of it (spec 6.3)."""
    live = [state.items[i] for i in state.fingerprints.get(fp, ()) if state.items[i].status == "live"] if fp else []
    return min(live, key=lambda i: (i.first_ts, i.id)).id if live else None


def plan(state: State, candidates: list[Candidate], shas: dict[str, str], exif: dict[str, dict],
         import_of: dict[str, str], clips: dict[str, Candidate],
         problems: list[dict]) -> tuple[list[Planned], dict[str, str]]:
    """The items the import adds or refers to again, in source order, and each candidate's outcome. shas, exif and
    import_of (the import id that records it) are by source path, for every candidate whose fate is settled now."""
    attached = {c.source_path for c in clips.values()}
    photos = [c for c in candidates if c.source_path in shas and c.source_path not in attached]
    meta = {c.source_path: derive(exif.get(c.source_path, {}), c.provider, c.name, c.mtime) for c in photos}
    live_files = {f["sha256"] for i in state.items.values() if i.status == "live" for f in i.files}
    gone = {f["sha256"] for i in state.items.values() if i.blocked for f in i.files}

    def blocked(sha: str, fp: tuple | None = None) -> bool:
        known = state.items.get(sha)
        # A known item is judged by its own state, as fold does: versions share fingerprints.
        return known.blocked if known else sha in gone or state.is_blocked(sha, fp)

    # Decided once per SHA-256: one copy's fingerprint can match a deleted photo while another copy has no date.
    deleted = {shas[p] for p, m in meta.items() if blocked(shas[p], fingerprint(m))}
    items: dict[str, Planned] = {}
    settled: set[str] = set()  # held already with nothing to log: recorded by this import, or not a main file
    first_with: dict[tuple, str] = {}  # fingerprint -> the first item this import adds with it
    outcome: dict[str, str] = {}

    def by_own_bytes(clip: tuple[Candidate, str], it: Planned | None, verdict: str) -> None:
        """A clip of a photo that adds no new item counts by its own bytes, which the library must hold already."""
        if clip[1] in live_files or it is not None and it.clip is not None and it.clip[1] == clip[1]:
            outcome[clip[0].source_path] = verdict
        else:
            outcome[clip[0].source_path] = "problem"
            problems.append(problem(clip[0], "clip not in library"))

    for c in photos:
        sha, found = shas[c.source_path], clips.get(c.source_path)
        clip = (found, shas[found.source_path]) if found and found.source_path in shas else None
        if sha in deleted:
            outcome.update(dict.fromkeys([c.source_path, *([clip[0].source_path] if clip else [])], "skipped_deleted"))
            continue
        if clip and blocked(clip[1]):
            outcome[clip[0].source_path] = "skipped_deleted"
            clip = None
        it = items.get(sha)
        if it or sha in settled:
            outcome[c.source_path] = "duplicates_in_source"
            if it:
                it.albums.update(c.albums)
                it.favorite, it.trashed = it.favorite or c.favorite, it.trashed and c.trashed
            if clip and it and not it.known and it.clip is None:  # a clip only this copy has is not lost
                it.clip, outcome[clip[0].source_path] = clip, "live_clips"
            elif clip:
                by_own_bytes(clip, it, "duplicates_in_source")
            continue
        known = state.items[sha] if sha in state.items and state.items[sha].status == "live" else None
        if known and import_of[c.source_path] in known.imports or not known and sha in live_files:
            settled.add(sha)
            outcome[c.source_path] = "already_in_library"
            if clip:
                by_own_bytes(clip, None, "already_in_library")
            continue
        m, fp = meta[c.source_path], fingerprint(meta[c.source_path])
        it = items[sha] = Planned(c, sha, import_of[c.source_path], folder_of(m["taken"]), list(fp) if fp else None,
                                known, set(c.albums), c.favorite, c.trashed and not known)
        if known:
            outcome[c.source_path] = "already_in_library"
            if clip:
                if clip[1] in {f["sha256"] for f in known.files if f["role"] == "live"}:
                    it.clip = clip
                by_own_bytes(clip, it, "already_in_library")
            continue
        outcome[c.source_path] = "imported"
        it.version_of = _version_of(state, fp) or (first_with.get(fp) if fp else None)
        if fp:
            first_with.setdefault(fp, sha)
        if clip:
            it.clip, outcome[clip[0].source_path] = clip, "live_clips"
    _link_edits(state, items, shas, problems)
    return list(items.values()), outcome


def _link_edits(state: State, items: dict[str, Planned], shas: dict[str, str], problems: list[dict]) -> None:
    """A new item that is an edit links to its original's item, when this import has it or the library holds it."""
    for it in items.values():
        original = it.main.edit_of
        target = shas.get(original) if original else None
        if it.known or original is None or target == it.sha:
            continue
        if target and (target in items or target in state.items and state.items[target].status == "live"):
            it.edit_of = target
        else:
            problems.append(problem(it.main, f"its original {original} was not imported: kept without the link"))


def events_of(lib: Library, items: list[Planned], adapter: str, start: datetime) -> list[dict]:
    """Every import first, then the marks: a trash folded before a later import of the same fingerprint would
    make fold ignore that import. A known item gets only the albums and favorite it lacks, never a trash. Each
    event a microsecond after the last keeps their order."""
    plain: list[tuple[str, dict]] = []
    for it in items:
        files = [{"role": role, "name": c.name, "sha256": sha, "size": c.size, "mtime": c.mtime}
                 for role, c, sha in it.files()]
        links = {k: v for k, v in (("version_of", it.version_of), ("edit_of", it.edit_of)) if v}
        plain.append(("import", {"item": it.sha, "import": it.import_id, "folder": it.folder, "files": files,
                                 "fingerprint": it.fingerprint, **links}))
    for it in items:
        have = it.known.albums if it.known else set()
        plain += [("album.add", {"item": it.sha, "kind": "album", "album": title})
                  for title in sorted(it.albums) if ("album", title) not in have]
        plain += [("favorite", {"item": it.sha})] if it.favorite and not (it.known and it.known.favorite) else []
        plain += [("trash", {"item": it.sha, "reason": f"Trashed in {adapter}"})] if it.trashed else []
    return [events.new_event(lib, op, ts=events.ts_str(start + timedelta(microseconds=k)), **fields)
            for k, (op, fields) in enumerate(plain)]


def summary(items: list[Planned], outcome: dict[str, str], logged: list[dict], problems: list[dict]) -> dict:
    count, ops = Counter(outcome.values()), Counter(e["op"] for e in logged)
    new = [it for it in items if not it.known]
    return {"imported": count["imported"], "versions": sum(bool(it.version_of) for it in new),
            "edits": sum(bool(it.edit_of) for it in new), "live_clips": count["live_clips"],
            "already_in_library": count["already_in_library"], "duplicates_in_source": count["duplicates_in_source"],
            "skipped_deleted": count["skipped_deleted"], "trashed": ops["trash"], "album_links": ops["album.add"],
            "favorites": ops["favorite"], "bytes": sum(c.size for it in new for _, c, _ in it.files()),
            "problems": problems}

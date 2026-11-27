"""What an import does, decided without touching disk: from the log, the folded state and each candidate's
SHA-256 and EXIF, the items it adds, the live ones it records another source of, what it skips, and the events
that say so.
"""
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
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
    note: str | None = None  # a problem the adapter found (a Takeout file without its sidecar), listed as one


@dataclass(frozen=True)
class SourceFile:
    """A provider's metadata file, kept verbatim as sources/<import id>/<path>."""
    path: str
    read: Callable[[], bytes]


@dataclass(frozen=True)
class Source:
    """One import's record of an item: an import event naming the copy that import holds."""
    copy: Candidate
    import_id: str
    folder: str
    fingerprint: list | None


@dataclass
class Planned:
    """An item the import adds, or a live one the library holds (known), with every import that records it
    here and the marks its copies carry."""
    sha: str
    known: Item | None
    sources: list[Source] = field(default_factory=list)  # the first one creates a new item
    albums: set[str] = field(default_factory=set)
    favorite: bool = False
    trashed: bool = False  # every copy is in the provider's trash; never for a known item
    clip: tuple[Candidate, str] | None = None  # a new item's Live clip and its SHA-256
    version_of: str | None = None
    edit_of: str | None = None

    @property
    def main(self) -> Candidate:
        return self.sources[0].copy

    def files(self) -> list[tuple[str, Candidate, str]]:
        """(role, candidate, SHA-256) of each file a new item puts in place, the main one first."""
        return [("main", self.main, self.sha), *([("live", *self.clip)] if self.clip else [])]


@dataclass(frozen=True)
class Touched:
    """Marks the log has ever set or cleared on an item, which a later source never sets again: a mark the owner
    took off stays off."""
    favorite: frozenset[str]  # items
    albums: frozenset[tuple[str, str]]  # (item, title)
    renamed: frozenset[str]  # titles an album was renamed from

    @classmethod
    def of(cls, log: Iterable[dict]) -> "Touched":
        favorite, albums, renamed = set(), set(), set()
        for e in log:
            if e["op"] in ("favorite", "unfavorite"):
                favorite.add(e["item"])
            elif e["op"] in ("album.add", "album.remove") and e["kind"] == "album":
                albums.add((e["item"], e["album"]))
            elif e["op"] == "album.rename" and e["kind"] == "album":
                renamed.add(e["album"])
        return cls(frozenset(favorite), frozenset(albums), frozenset(renamed))

    def album(self, item: str, title: str) -> bool:
        return (item, title) in self.albums or title in self.renamed


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


def _unread(candidates: list[Candidate], unread: dict[str, str], shas: dict[str, str],
            clips: dict[str, Candidate]) -> list[dict]:
    """The files that could not be read, saying what became of a readable partner: it is imported alone."""
    photo_of = {clip.source_path: photo for photo, clip in clips.items()}
    edited = {c.edit_of for c in candidates if c.edit_of and c.source_path in shas}
    out = []
    for c in candidates:
        if c.source_path not in unread:
            continue
        reason = unread[c.source_path]
        if photo_of.get(c.source_path) in shas:
            reason += "; photo imported without it"
        elif c.source_path in edited:
            reason += "; its edit imported without the link"
        out.append(problem(c, reason))
    return out


def _version_of(state: State, fp: tuple | None) -> str | None:
    """The first live item with this fingerprint: the candidate is another version of it (spec 6.3)."""
    live = [state.items[i] for i in state.fingerprints.get(fp, ()) if state.items[i].status == "live"] if fp else []
    return min(live, key=lambda i: (i.first_ts, i.id)).id if live else None


def plan(state: State, candidates: list[Candidate], shas: dict[str, str], exif: dict[str, dict],
         import_of: dict[str, str], clips: dict[str, Candidate], unread: dict[str, str],
         problems: list[dict]) -> tuple[list[Planned], dict[str, str]]:
    """The items the import adds or records another source of, in source order, and each candidate's outcome.

    shas, exif and import_of (the import id that records it) are by source path, for every candidate read;
    unread holds why each of the others could not be.
    """
    problems += [problem(c, c.note) for c in candidates if c.note]
    problems += _unread(candidates, unread, shas, clips)
    attached = {c.source_path for c in clips.values()}
    meta = {c.source_path: derive(exif.get(c.source_path, {}), c.provider, c.name, c.mtime)
            for c in candidates if c.source_path in shas}
    live_files = {f["sha256"] for i in state.items.values() if i.status == "live" for f in i.files}
    gone = {f["sha256"] for i in state.items.values() if i.blocked for f in i.files}

    def blocked(sha: str, fp: tuple | None = None) -> bool:
        known = state.items.get(sha)
        # A known item is judged by its own state, as fold does: versions share fingerprints.
        return known.blocked if known else sha in gone or state.is_blocked(sha, fp)

    # Decided once per SHA-256: one copy's fingerprint can match a deleted photo while another copy has no date.
    deleted = {shas[p] for p, m in meta.items() if p not in attached and blocked(shas[p], fingerprint(m))}
    items: dict[str, Planned] = {}
    met: set[str] = set()  # SHA-256 of every copy taken so far
    recorded: set[tuple[str, str]] = set()  # (SHA-256, import id) whose source and marks the log holds already
    first_with: dict[tuple, str] = {}  # fingerprint -> the first item this import adds with it
    alone: list[Candidate] = []  # clips that no item can take: kept as items of their own
    outcome: dict[str, str] = {}

    def held_clip(clip: tuple[Candidate, str], live: str | None, verdict: str) -> None:
        """The clip of a photo whose item exists already counts by its own bytes. An item without a clip cannot
        take one later, so the clip becomes an item of its own; one with another clip keeps that one."""
        if clip[1] in live_files or clip[1] == live:
            outcome[clip[0].source_path] = verdict
        elif live is None:
            alone.append(clip[0])
        else:
            outcome[clip[0].source_path] = "problem"
            problems.append(problem(clip[0], "clip not in library"))

    def take(c: Candidate, clip: tuple[Candidate, str] | None, own_clip: bool = False) -> None:
        sha, import_id, m = shas[c.source_path], import_of[c.source_path], meta[c.source_path]
        fp = fingerprint(m)
        if sha in deleted or blocked(sha, fp):
            outcome.update(dict.fromkeys([c.source_path, *([clip[0].source_path] if clip else [])], "skipped_deleted"))
            return
        if clip and blocked(clip[1]):
            outcome[clip[0].source_path] = "skipped_deleted"
            clip = None
        known = state.items[sha] if sha in state.items and state.items[sha].status == "live" else None
        it, first = items.get(sha), sha not in met
        met.add(sha)
        verdict = ("already_in_library" if known or sha in live_files else "imported") if first \
            else "duplicates_in_source"
        outcome[c.source_path] = verdict
        key = (sha, import_id)
        if key in recorded or known and import_id in known.imports or not known and not it and sha in live_files:
            recorded.add(key)  # this import's record of the bytes is in the log, or there is no item to record
        else:
            if it is None:
                it = items[sha] = Planned(sha, known, trashed=c.trashed and not known)
                if own_clip and not known:
                    problems.append(problem(c, "clip kept as its own item"))
                if not known:
                    it.version_of = _version_of(state, fp) or (first_with.get(fp) if fp else None)
                    if fp:
                        first_with.setdefault(fp, sha)
            if key not in {(sha, s.import_id) for s in it.sources}:
                it.sources.append(Source(c, import_id, folder_of(m["taken"]), list(fp) if fp else None))
            it.albums.update(c.albums)
            it.favorite, it.trashed = it.favorite or c.favorite, it.trashed and c.trashed
        if clip is None:
            return
        if it and not it.known:  # an item this import adds: a clip only one copy has is not lost
            if it.clip is None:
                it.clip, outcome[clip[0].source_path] = clip, "live_clips"
            elif it.clip[1] == clip[1]:
                outcome[clip[0].source_path] = "duplicates_in_source"
            else:
                outcome[clip[0].source_path] = "problem"
                problems.append(problem(clip[0], "clip not in library"))
        else:
            live = next((f["sha256"] for f in known.files if f["role"] == "live"), None) if known else None
            held_clip(clip, live, verdict)

    for c in candidates:
        found = clips.get(c.source_path)
        if c.source_path in shas and c.source_path not in attached:
            take(c, (found, shas[found.source_path]) if found and found.source_path in shas else None)
        elif found and found.source_path in shas and c.source_path not in shas:  # the photo could not be read
            alone.append(found)
    for clip in alone:
        take(clip, None, own_clip=True)
    _link_edits(state, items, shas, unread, problems)
    return list(items.values()), outcome


def _link_edits(state: State, items: dict[str, Planned], shas: dict[str, str], unread: dict[str, str],
                problems: list[dict]) -> None:
    """A new item that is an edit links to its original's item, when this import has it or the library holds it."""
    for it in items.values():
        original = it.main.edit_of
        target = shas.get(original) if original else None
        if it.known or original is None or original in unread or target == it.sha:
            continue  # an unreadable original is listed with its edit's fate
        if target and (target in items or target in state.items and state.items[target].status == "live"):
            it.edit_of = target
        else:
            problems.append(problem(it.main, f"its original {original} was not imported: kept without the link"))


def events_of(lib: Library, items: list[Planned], adapter: str, start: datetime, touched: Touched) -> list[dict]:
    """Every import first, then the marks: a trash folded before a later import of the same fingerprint would
    make fold ignore that import. An item the library holds gets only marks the log never set or cleared on it,
    and never a trash. Each event a microsecond after the last keeps their order."""
    plain: list[tuple[str, dict]] = []
    for it in items:
        for n, s in enumerate(it.sources):
            creates = n == 0 and not it.known
            files = [{"role": role, "name": c.name, "sha256": sha, "size": c.size, "mtime": c.mtime}
                     for role, c, sha in (it.files() if creates else [("main", s.copy, it.sha)])]
            links = {k: v for k, v in (("version_of", it.version_of), ("edit_of", it.edit_of)) if v and creates}
            plain.append(("import", {"item": it.sha, "import": s.import_id, "folder": s.folder, "files": files,
                                     "fingerprint": s.fingerprint, **links}))
    for it in items:
        albums = sorted(t for t in it.albums if not it.known or ("album", t) not in it.known.albums
                        and not touched.album(it.sha, t))
        plain += [("album.add", {"item": it.sha, "kind": "album", "album": title}) for title in albums]
        if it.favorite and not (it.known and it.sha in touched.favorite):
            plain.append(("favorite", {"item": it.sha}))
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

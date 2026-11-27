from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from photokeep.events import ts_str

TRASH_DAYS = 30


@dataclass
class Item:
    id: str
    folder: str
    files: list[dict]
    imports: list[str]
    first_ts: str
    fingerprint: tuple | None
    version_of: str | None
    status: str = "live"  # live | trashed | purged
    trashed_at: str | None = None
    blocked: bool = False
    favorite: bool = False
    hidden: bool = False
    albums: set[tuple[str, str]] = field(default_factory=set)
    trash_reason: str | None = None


@dataclass
class State:
    items: dict[str, Item] = field(default_factory=dict)
    copies: dict[str, str] = field(default_factory=dict)
    tombstones: dict[str, str] = field(default_factory=dict)
    fingerprints: dict[tuple, set[str]] = field(default_factory=dict)

    def apply(self, e: dict) -> None:
        # Events arrive in (ts, copy, id) order; every fold rule lives in this method.
        op = e["op"]
        item = self.items.get(e["item"]) if "item" in e else None
        match op:
            case "copy.name":
                self.copies[e["copy"]] = e["name"]
            case "import":
                self._import(e, item)
            case "trash" if item and item.status == "live":
                item.status, item.trashed_at, item.blocked = "trashed", e["ts"], True
                item.trash_reason = e.get("reason")
            case "restore" if item and item.status == "trashed":
                item.status, item.trashed_at, item.blocked = "live", None, False
                item.trash_reason = None
            case "purge":
                if item:
                    item.status, item.blocked = "purged", True
                else:
                    self.tombstones[e["item"]] = e.get("name", "")
            case "readmit":
                if item and item.status == "purged":
                    item.blocked = False
                self.tombstones.pop(e["item"], None)
            case "favorite" | "unfavorite" if item:
                item.favorite = op == "favorite"
            case "hide" | "unhide" if item:
                item.hidden = op == "hide"
            case "album.add" if item:
                item.albums.add((e["kind"], e["album"]))
            case "album.remove" if item:
                item.albums.discard((e["kind"], e["album"]))
            case "album.rename":
                for it in self.items.values():
                    if (e["kind"], e["album"]) in it.albums:
                        it.albums.remove((e["kind"], e["album"]))
                        it.albums.add((e["kind"], e["to"]))

    def _import(self, e: dict, item: Item | None) -> None:
        fingerprint = tuple(e["fingerprint"]) if e.get("fingerprint") else None
        # A known item is judged by its own state only: versions share fingerprints.
        if item.blocked if item else self.is_blocked(e["item"], fingerprint):
            return
        if item and item.status == "live":
            item.imports.append(e["import"])
            return
        # New, or a purged item that was readmitted: it starts over but keeps its marks.
        fresh = Item(e["item"], e["folder"], e["files"], [e["import"]], e["ts"], fingerprint, e.get("version_of"))
        if item:
            fresh.favorite, fresh.hidden, fresh.albums = item.favorite, item.hidden, item.albums
        self.items[e["item"]] = fresh
        if fingerprint:
            self.fingerprints.setdefault(fingerprint, set()).add(e["item"])

    def live(self) -> list[Item]:
        return [i for i in self.items.values() if i.status == "live"]

    def is_blocked(self, sha: str, fingerprint: tuple | None = None) -> bool:
        if sha in self.tombstones or (sha in self.items and self.items[sha].blocked):
            return True
        return fingerprint is not None and any(self.items[i].blocked for i in self.fingerprints.get(fingerprint, ()))

    def due_purges(self, now: datetime, days: int = TRASH_DAYS) -> list[str]:
        cutoff = ts_str(now - timedelta(days=days))
        return sorted(i.id for i in self.items.values()
                      if i.status == "trashed" and i.trashed_at is not None and i.trashed_at <= cutoff)


def fold(events: Iterable[dict]) -> State:
    state = State()
    for e in events:
        state.apply(e)
    return state

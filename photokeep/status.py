from photokeep.events import read_all
from photokeep.library import Library
from photokeep.state import fold


def status(lib: Library) -> dict:
    events = read_all(lib)
    state = fold(events)
    live = state.live()
    return {
        "library_id": lib.library_id,
        "copy_id": lib.copy_id,
        "copy_name": lib.copy_name,
        "items_live": len(live),
        "items_trashed": sum(i.status == "trashed" for i in state.items.values()),
        "bytes_live": sum(f["size"] for i in live for f in i.files),
        "copies": dict(state.copies),
        "last_event_ts": max((e["ts"] for e in events), default=None),
    }

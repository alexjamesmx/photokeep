# Library format (version 1)

A library is a set of equal copies. Each copy is a folder; copies converge by exchanging change logs and files.

## Layout

```
<root>/
  library.json   shared by all copies
  copy.json      this copy only, never synced
  originals/     YYYY/MM/<name>, or undated/<name>
  sources/       untouched imports (opaque to the app)
  log/           <copy_id>.jsonl, one change log per copy
  trash/         <item id>/<name>: trashed items, <name> is the name the item has (or would have) in originals/
  cache/         derived data (thumbnails, hashes); safe to delete
  app/           photokeep/: a copy of the app, so the copy opens on any computer with Python 3.11+
  Open Photos.sh, Open Photos.command, Open Photos.bat   start the viewer from app/ (Linux, macOS, Windows)
```

The viewer records every change it makes (favorite, trash, restore, delete forever) as an event in the copy's log, then moves the files as sync would.

Restore moves a file back to the same place in originals/. Originals and trash files are only renamed, copied with hash verification, or deleted when their item is purged. Their bytes are never modified.

Sync copies `sources/` too: each `sources/<import id>/` folder is write-once, so files are only added, and a path whose bytes differ between copies is reported as a conflict and left alone.

## JSON files

- `library.json`: `{"format": 1, "library_id": "<uuid4 hex>", "created": "<utc iso>"}`. A reader refuses `format` greater than it supports.
- `copy.json`: `{"copy_id": "<uuid4 hex>", "name": "<name>", "created": "<utc iso>"}`.

Timestamps are UTC, `YYYY-MM-DDTHH:MM:SS.ffffffZ`. Both files are written atomically (temp file, then rename).

## Naming rules

- Names are NFC-normalized, unique case-insensitively within a folder (compared as `safe_name(n).casefold()`), contain none of `< > : " / \ | ? *` or control characters, and have no trailing dot or space (exFAT).
- Assignment iterates non-purged items sorted by `(first_ts, id)`. A file gets `safe_name(original)` if free, else `<stem>~<item id[:8]><ext>`. A Live clip follows the same rule independently.
- A name freed by a purge is never reused within the same assignment run (purged items are skipped).

## Change log

One file per copy, append-only. A line is one JSON object plus `\n`. Readers ignore a torn last line (no trailing newline) and the next append cuts it off; a bad line elsewhere is corruption. Unknown ops are kept when reading and ignored when folding.

Common keys: `v` (1), `id` (uuid4 hex), `ts` (UTC, as above), `copy` (author's copy id), `op`, then op fields. Events are ordered by `(ts, copy, id)`. The item id is the lowercase hex SHA-256 of the main file.

| op | fields |
|---|---|
| `copy.name` | `name` |
| `import` | `item`, `import` (import id), `folder` (`YYYY/MM` or `undated`), `files` (list of `{role: "main"\|"live", name, sha256, size, mtime}`), `fingerprint` (`[taken, model, w, h]` or null), `version_of` (sha or null) |
| `favorite`, `unfavorite`, `hide`, `unhide`, `restore`, `readmit` | `item` |
| `trash` | `item`, optional `reason` |
| `purge` | `item`, optional `name` |
| `album.add`, `album.remove` | `item`, `album`, `kind` (`album` or `memory`) |
| `album.rename` | `album`, `to`, `kind` |

### Fold rules

- First `import` creates a live item; a later `import` of a live item appends its import id; an `import` of a trashed, purged or blocked item is ignored.
- `trash` makes the item trashed (records `trashed_at`, sets `blocked`). `restore` of a trashed item makes it live and unblocked.
- `purge` of any item makes it purged and blocked, and is final. For an unknown item it creates a tombstone (`tombstones[id] = name`).
- `readmit` clears `blocked` on a purged item or tombstone, so a later `import` makes it live again.
- `favorite`, `hide` and album events apply to any item. `album.rename` renames `(kind, album)` on every item.
- `copy.name` fills the copy table.
- An item is blocked if its id, or a fingerprint belonging to a blocked item, matches.

## Trash

Trashed items stay in `trash/` for `TRASH_DAYS = 30` from the `trash` event. At the next sync, items past that age get a `purge` event and their files are deleted.

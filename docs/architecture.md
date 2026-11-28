# Architecture

## Three layers of truth

1. **Files** under `originals/` and `trash/`: the photos themselves, never modified. An item's id is the SHA-256 of its main file.
2. **Change logs** under `log/`: one append-only `<copy-id>.jsonl` per copy. Each copy writes only its own log. Folding all logs gives the state: items, favorites, albums, trash, purges.
3. **Sources** under `sources/<import-id>/`: verbatim provider files (CSVs, EXIF dumps), write-once. Display metadata is derived from them and never written back.

`cache/` (viewer index, thumbnails, hash cache) is derived from the three and safe to delete. `app/` is a copy of the program.

```
 copy A                         copy B
 log/A.jsonl  --- sync --->     log/A.jsonl
 log/B.jsonl  <--- sync ---     log/B.jsonl
      \                            /
       fold, ordered (ts, copy, id)
                  |
        state: where each file belongs
                  |
     rename / copy files to match, per copy
```

## Events

An event is one JSON line: `v`, `id`, `ts`, `copy`, `op`, plus op fields (see [library-format.md](library-format.md)). Events from all logs are ordered by `(ts, copy, id)` and folded in that order, so every copy reaches the same state from the same logs.

- Latest wins: a later `favorite` or `unfavorite` overrides an earlier one.
- `purge` is final. A purged item and its hash stay blocked, so no later `import` brings it back. Only an explicit `readmit` lifts the block.
- No resurrection: an `import` of a trashed, purged or blocked item is ignored. `restore` only works on a trashed item.
- Unknown ops are kept and ignored, so a newer copy's log never breaks an older reader.

The log says what should exist and where. Files are moved to match it, never the reverse.

## Sync phases

`photokeep sync` (in `sync.py`) takes both copies' locks, then:

1. Exchange logs: append to each side the lines the other has. Logs only grow.
2. Write `purge` events for items in the trash for 30 days or more, then exchange logs again.
3. Copy `sources/` files that one side lacks. A path with different bytes on each side is a conflict and is left alone.
4. Arrange: on each side, rename files to the names the folded state assigns and delete files of purged items.
5. Copy files that one side lacks from the other, with hash verification.
6. With `--update-app`, replace the older `app/` with the newer.
7. Refresh each copy's index (no thumbnails).

Every step is idempotent, so an interrupted sync is finished by running it again. Without `--yes`, the same code only plans.

## Import

`photokeep import <adapter> <source>... --into <copy>` (in `importer.py`) turns a source into events. An adapter (`adapters/takeout.py`, `adapters/folder.py`) only lists candidate files and the provider files to keep. The shared pipeline then:

1. stages every candidate under `cache/import/`, hashing it and reading its EXIF with exiftool,
2. plans: skips what the library deleted (trashed, purged, or the same photo re-encoded), records another source for what it already holds, links versions, edits and Live clips (`importplan.py`),
3. writes `sources/<import-id>/` (provider files verbatim, `exif.json`, `manifest.json`), moves the files into `originals/` and `trash/`, and appends all events in one append, last.

Files go first and the log last, so an interrupted import is finished by running it again. A file that could not be read is left for a later run. `--dry-run` runs steps 1 and 2 and removes its staging.

## Cache

`cache/index.sqlite` holds one display row per item, derived from the folded log, `sources/` and the files. `cache/files.sqlite` remembers file hashes while size and mtime are unchanged. Thumbnails and previews are rendered by `photokeep cache refresh` and the viewer; sync copies them to a copy that lacks them, since both hold the same originals. A damaged cache is deleted and rebuilt; no data lives only there.

## Viewer write path

`photokeep serve` listens on 127.0.0.1 only. A change (trash, restore, delete forever, favorite) is a POST with a per-run token. The server, under the copy's lock:

1. folds the current logs,
2. appends the event to this copy's log,
3. updates the index,
4. moves the files, as sync would.

Index before files, so a failed move leaves the index right and a retry finishes the move. The viewer never touches another copy; the change reaches it at the next sync.

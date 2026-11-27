# 0001: Plain folders plus per-copy change logs

## Context

Photos must outlive any app or provider. Copies live on drives that may be offline for months and are edited independently. A central database, or a "master" copy, would be a single point of failure and would need to be reachable.

## Decision

A copy is a plain folder tree (`originals/YYYY/MM/<name>`), readable without Photokeep. Changes (imports, favorites, trash, purges) are append-only events in one log per copy, `log/<copy-id>.jsonl`. Each copy writes only its own log. Sync exchanges logs and files; the folded events decide where files belong.

## Consequences

- Any file browser, backup tool or other software can read a copy.
- No merge conflicts in logs: two copies never write the same file. Ordering `(ts, copy, id)` makes folds identical everywhere.
- Offline edits merge when copies meet; latest event wins.
- Copies need a roughly correct clock; a far-future timestamp wins until later events outrank it.
- Logs only grow. Fine for photos: one line per change.
- The viewer's index is derived and can always be rebuilt.

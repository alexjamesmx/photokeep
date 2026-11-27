# 0003: One global trash, kept 30 days

## Context

Deleting in the viewer on one drive must not silently lose a photo that other copies still hold, and mistakes must be undoable. Each copy sees changes only at sync.

## Decision

Deleting writes a `trash` event. The item moves to `trash/<item id>/<name>` on every copy at the next sync, and `restore` brings it back to its place in `originals/`. After `TRASH_DAYS = 30` from the `trash` event, the next sync writes a `purge` event and deletes the files. Trash is part of the library, not a per-copy folder.

## Consequences

- A mistake is recoverable for 30 days from any copy.
- Space is freed only after the 30 days and a sync.
- A trashed item blocks re-import, so a cloud re-download does not revive it.
- "Delete forever" in the viewer is a `purge` event, final under [0002](0002-no-resurrection.md).
- Sync with new purges deletes files, so agents ask the owner first.

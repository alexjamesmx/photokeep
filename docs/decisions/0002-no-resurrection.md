# 0002: Purge is final; nothing is resurrected

## Context

Photos are imported from several places (cloud exports, phones, drives) and the same photo can arrive again. If the owner deletes a photo for good, a later import or an offline copy that still holds it must not bring it back.

## Decision

`purge` is final. A purged item stays blocked by its hash and by a matching fingerprint (taken time, model, dimensions). An `import` of a trashed, purged or blocked item is ignored. Only an explicit `readmit` event lifts a block. Purging an item this copy never saw leaves a tombstone, so it is blocked on arrival.

## Consequences

- Deleting once is enough, on every copy, after the next sync.
- A copy that was offline cannot undo a deletion made elsewhere.
- Re-importing a deliberately deleted photo needs `readmit`, which an agent runs only when the owner asks.
- Purged bytes are deleted from every copy; this is the only time an original is removed.
- A wrong purge is permanent for the files. Hence the 30-day trash, and the owner's confirmation before sync deletes.

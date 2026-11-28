# Sync two copies

Use when the owner asks to sync, back up or bring two copies level. Both copies must be mounted. Find copies by `copy.json`, never by mount path.

1. Check both copies open and belong to the same library:
   ```
   photokeep status <copy-a> --json
   photokeep status <copy-b> --json
   ```
   Success: exit 0 for both, and the same `library_id`. Exit 1 means the path is not a copy or the copies differ: stop and ask.
2. Show the plan (this changes nothing):
   ```
   photokeep sync <copy-a> <copy-b> --json
   ```
   Exit 2 means the plan already shows a problem (see step 5 for the fields): stop and ask the owner. Otherwise read the fields:
   - `copy_a_to_b`, `copy_b_to_a`: files that will be copied; `bytes_a_to_b`, `bytes_b_to_a`: the space they take.
   - `new_purges`: items whose 30 days in the trash are over; their files will be deleted.
   - `layout_a.deletes`, `layout_b.deletes`: files that will be deleted. `moves` are renames.
   - `app.direction`: not null when the two apps differ in version (see [update-app](update-app.md)).
3. Stop and ask the owner before step 4 if `new_purges` is not empty or any `layout_*.deletes` is above 0. Show them the counts. Otherwise go on.
4. Carry it out:
   ```
   photokeep sync <copy-a> <copy-b> --yes --json
   ```
   Success of this `--yes` run (a plan has `pending` null): exit 0, `missing_both` 0, `conflicts` 0, `bad_sources` empty, `sources.conflicts` empty, `failed` empty, and `pending` shows `moves`, `deletes`, `missing`, `untracked` and `conflicts` all 0 for both `a` and `b`. An interrupted sync is finished by running it again.
5. If the exit code is 2, the copies could not be made whole. Stop and ask the owner. Do not retry or delete anything. Report which field is non-zero:
   - `missing_both`: files the log expects that neither copy holds. A third copy may have them.
   - `bad_sources`: files in `sources/` that failed their hash, skipped.
   - `sources.conflicts`: a `sources/` path whose bytes differ between copies, left alone.
   - `conflicts`: a path holds different bytes than the log says.
   - `failed`: files a copy's disk refused to write, move or delete, each with its reason (a name the drive cannot hold, a read error). The rest was synced. A full disk stops the sync instead, with exit 1.
6. Verify each copy (see [verify-repair](verify-repair.md)) when the owner wants proof:
   ```
   photokeep verify <copy-a> --json
   photokeep verify <copy-b> --json
   ```
   Success: `"ok": true`.

Sync copies the thumbnails and previews one copy rendered to the other. Run `photokeep cache refresh <copy>` to render any still missing.

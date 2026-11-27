# Verify and repair a copy

Use for a periodic check, after a drive error, or when a copy looks wrong.

1. Check a copy:
   ```
   photokeep verify <copy> --json
   ```
   Add `--sample <N>` to rehash only N random files for a quick check. Success: exit 0 and `"ok": true`, with `missing`, `corrupt` and `untracked` empty. A sample proves less than a full run.
2. If exit 2, read the lists:
   - `missing`: files the log expects but the copy lacks.
   - `corrupt`: files whose hash does not match the log.
   - `untracked`: files the log does not know. They may be stray. Do not delete them without asking the owner.
3. Repair only `missing` and `corrupt` files. Stop and ask the owner before `repair`. Name the damaged copy and the healthy copy to restore from. First check that the source copy is healthy:
   ```
   photokeep verify <good-copy> --json
   ```
   Success: `"ok": true`.
4. After the owner agrees:
   ```
   photokeep repair <copy> --from <good-copy> --json
   ```
   Corrupt files are moved to `cache/quarantine/<stamp>/` first and kept there, never deleted. Success: exit 0 and `"ok": true` in the fresh report it prints.
5. If exit 2 after repair, the listed files are bad in the source too. Look for a third copy and repeat step 4 with it. If there is none, report the files to the owner.
6. Quarantined files are the owner's to delete. Do not remove `cache/quarantine/` yourself.

If the index looks wrong, `photokeep cache refresh <copy>` rebuilds it; `cache/` holds only rebuildable data.

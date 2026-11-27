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
4. Show the owner what repair would do (this changes nothing):
   ```
   photokeep repair <copy> --from <good-copy> --dry-run --json
   ```
   `restore` lists the files the good copy holds right bytes for; `no_source` lists the files it does not. Exit 2 means the copy would not be whole afterwards: some files have no good copy, or untracked files remain.
5. After the owner agrees:
   ```
   photokeep repair <copy> --from <good-copy> --json
   ```
   Each replacement is copied and checked before the corrupt file moves, and the corrupt file is kept in `quarantine/<run>/` at the copy's root (`quarantine` in the output), never deleted. A corrupt file with no good copy in `<good-copy>` stays where it is. Success: exit 0 and `"ok": true`.
6. If exit 2 after repair, read `no_source` and `failed`: those files are bad or missing in the source too, or could not be written. Look for a third copy and repeat steps 4 and 5 with it. If there is none, report the files to the owner.
7. Quarantined files are the owner's to delete. Do not remove `quarantine/` yourself.

If the index looks wrong, `photokeep cache refresh <copy>` rebuilds it; `cache/` holds only rebuildable data.

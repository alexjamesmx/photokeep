# Add a drive as a new copy

Use when the owner wants an external drive to hold a copy of the library. The drive's filesystem should be exFAT or similar. Names in a library avoid `< > : " / \ | ? *` and trailing dots or spaces, so any filesystem works.

1. Find the existing copy to join, by its `copy.json`. Confirm it:
   ```
   photokeep status <existing-copy> --json
   ```
   Success: exit 0.
2. Pick the target folder on the drive, e.g. `<drive>/Photos`. It must not exist or must be empty. `init` refuses anything else and changes nothing. Do not delete or empty a folder to make room; ask the owner.
3. Create the copy:
   ```
   photokeep init <drive>/Photos --name <copy-name> --copy-of <existing-copy> --json
   ```
   Success: exit 0 and a JSON object with `root`, `library_id` (equal to the existing copy's) and a new `copy_id`. The new copy also gets `app/` and the `Open Photos` launchers.
4. Fill it by syncing, following [sync](sync.md) with the existing copy as `<copy-a>` and `<drive>/Photos` as `<copy-b>`. The first plan only copies files to the drive, so there should be no deletes.
5. Verify the drive:
   ```
   photokeep verify <drive>/Photos --json
   ```
   Success: exit 0 and `"ok": true`.
6. Tell the owner the drive is a full copy; unmounting it is safe once the sync has finished.

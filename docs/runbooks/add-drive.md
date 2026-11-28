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
   Success: exit 0 and a JSON object with `root`, `library_id` (equal to the existing copy's) and a new `copy_id`. The new copy also gets `app/` and the `Open Photos` launchers. A warning that it is on the same disk as the existing copy usually means the drive is not mounted: stop and ask the owner.
4. Check the drive has room. The plan changes nothing:
   ```
   photokeep sync <existing-copy> <drive>/Photos --json
   ```
   `bytes_a_to_b` is what the photos take on the drive. Read the drive's free space (`df -k <drive>` on Linux and macOS gives KiB). Go on only if it is above `bytes_a_to_b` with a few percent to spare for `sources/` and the thumbnails; otherwise stop and ask the owner.
5. Fill it by syncing, following [sync](sync.md) with the existing copy as `<copy-a>` and `<drive>/Photos` as `<copy-b>`. The first plan only copies files to the drive, so there should be no deletes.
6. Verify the drive:
   ```
   photokeep verify <drive>/Photos --json
   ```
   Success: exit 0 and `"ok": true`.
7. Render any thumbnails sync could not copy from the existing copy (usually none; quick):
   ```
   photokeep cache refresh <drive>/Photos
   ```
   Success: exit 0.
8. Tell the owner the drive is a full copy; unmounting it is safe once these commands have finished.

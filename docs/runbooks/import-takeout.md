# Import a Google Takeout

Use when the owner has exported their Google Photos with Google Takeout and wants the photos in a copy. The import only READS the zips, so it needs free space on the copy for the photos (they are staged under `cache/import/` first, then moved into `originals/`). It skips photos the library deleted, and it records photos the library already holds as one more source instead of adding them twice. Needs `exiftool` installed (`exiftool -ver`); the import refuses to start without it.

Needs:
- `<zip>...`: every part of the Takeout (`takeout-...-001.zip`, `-002.zip`, ...), or folders they were extracted into that hold `Takeout/Google Photos/`. A part missing means its photos and sidecars are missing.
- `<copy>`: an existing copy, on a disk with room for the photos: about the Takeout's size (the zips' total) plus 1 GiB free.

1. Download every part of the export and check none is missing: the part numbers run from 001 without a gap, and each file opens (`unzip -tq <zip>` prints `No errors detected`). Do not delete the zips until step 8.
2. Dry run. It reads every zip and runs exiftool (minutes), staging every photo under `<copy>/cache/import/` and removing them after, and changes nothing else. Staging needs free space about the Takeout's size before the dry run too: check with `df -h <copy>` that the copy's disk has the zips' total plus 1 GiB free, and ask the owner to free space otherwise. Then:
   ```
   photokeep import takeout <zip>... --into <copy> --dry-run --json
   ```
   Progress goes to stderr. Exit 1 prints one line saying why (no exiftool, too little free space to stage, a part that is not a zip or lacks `Takeout/Google Photos/`, the same file in two parts with different bytes, a file name that is not UTF-8, the copy inside a source): fix that, do not work around it.
3. Read the counts. Success is exit 0 with `problems` empty. Stop and ask the owner before going on when:
   - exit is 2: `problems` lists each file as `source_path: reason`. Show the list. `no sidecar` and `sidecar too large` files import with no Google date or place; `could not be read` files are left for a later run once the zip is fixed.
   - `skipped_deleted` is larger than the owner expects. It counts photos the library deleted earlier (trashed or purged), including a re-encoded copy of one. Many on a first import means something was deleted that the owner wants back: `readmit` is the owner's decision, never yours.
   - `imported` plus `already_in_library` plus `duplicates_in_source` plus `skipped_deleted` plus `live_clips` is far from the number of photos and videos in Google Photos.
   - `trashed` is not what the owner expects: those are in Google's trash and are imported, then trashed here (30 days before the purge).
   - `bytes` is more than the copy's free space.
   Otherwise the counts are expected: `versions` and `edits` link another version or an `-edited` copy to its photo; `album_links` and `favorites` come from Google.
4. Run it for real, with the same arguments and no `--dry-run`:
   ```
   photokeep import takeout <zip>... --into <copy> --json
   ```
   Success: exit 0, `problems` empty, and `imported` equal to the dry run's. Exit 2 again lists `problems`: show them and stop. If it was cut off (Ctrl+C, power loss), run the same command again; it finishes the import and adds nothing twice.
5. Verify:
   ```
   photokeep verify <copy> --json
   ```
   Success: exit 0 and `"ok": true`.
6. Look at the photos, and ask the owner to look too. The viewer runs until Ctrl+C, so start it where it can keep running (or ask the owner to run it), and open the address it prints:
   ```
   photokeep serve <copy> --no-browser
   ```
   Stop it with Ctrl+C before step 7.
7. Sync to the other copies, following [sync](sync.md) for each. A copy that only receives should show no deletes; stop and ask otherwise.
8. The zips may be deleted only after step 5 is ok and every media file in them is in the library or skipped as deleted. Check with a dry run of the same zips, never a real run (one with another set of zips would be a new import, recording thousands of photos again as one more source):
   ```
   photokeep import takeout <zip>... --into <copy> --dry-run --json
   ```
   It must give `"imported": 0` and an empty `problems`, which means each file is `already_in_library`, `duplicates_in_source` or `skipped_deleted`. `trashed`, `album_links` and `favorites` must be 0 too; if not, a run was cut off while writing the log: run the step 4 command again with exactly the same zips, then this check. Even then, ask the owner before deleting the zips, and never delete anything yourself. The owner may also want to keep them as a second backup until a drive holds a verified copy.

## Plain folders

`photokeep import folder <folder>... --into <copy> [--dry-run]` imports every photo and video under the folders with the same steps (2 to 7). It keeps no provider metadata: a photo is dated by its EXIF, else by its file's modified date, and its source path is `<folder name>/<path inside it>`. Two folders with the same name are refused. Use it for folders of photos, not for an export from a service.

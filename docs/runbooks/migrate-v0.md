# Migrate the prototype library (once)

Use once, when the owner has a prototype library (a folder with `.library/library.db`) and the matching Apple iCloud export. It COPIES with hash verification and never changes the prototype, the export or its zips, so it needs free space for a second copy of the photos. The owner decides about deleting the old files later; do not delete them.

Needs:
- `<old>`: the prototype library folder.
- `<raw>`: the export's extracted `raw/` folder (holds `partN/...`), with `exif.json` beside it.
- `<zip>...`: the export's zip files. Media in them that the library no longer has is blocked, so the phone backup cannot bring it back.
- `<new-copy>`: a new, empty folder outside `<old>` and `<raw>`.

1. Create the destination copy:
   ```
   photokeep init <new-copy> --name <copy-name> --json
   ```
   Success: exit 0.
2. Check free space on the destination is above the library's size. Stop and ask if unsure.
3. Ask the owner to stop the prototype's viewer, so nothing changes the prototype while it is read. Then dry run (changes nothing, still hashes everything, takes minutes):
   ```
   photokeep migrate-v0 <old> <new-copy> --raw <raw> --zip <zip>... --dry-run --json
   ```
   Progress goes to stderr. Exit 2 lists `problems`: show them to the owner and stop. On exit 0, check before going on, and stop and ask the owner if any is off:
   - `items`, `trashed`, `favorites`, `album_links`, `tombstones` match what the owner expects.
   - `exif_matched` and `provider_matched` are close to `items`: each item found its exiftool record and its Photo Details row. Far lower means the metadata was not matched and dates and fingerprints would be lost.
   - `date_differs_from_prototype` is 0 or close to it: dates the prototype showed, compared to the minute.
   - `clip_name_guessed` is small: Live Photo clips whose original name was not found by their ContentIdentifier.
   - `merged` lists rows with the same bytes as another row; they become one item with all their marks. It needs no action.
4. Run it for real, with the same arguments and no `--dry-run`:
   ```
   photokeep migrate-v0 <old> <new-copy> --raw <raw> --zip <zip>... --json
   ```
   Success: exit 0 and `problems` empty. If cut off, a second run refuses a copy that already has items. Follow the message it prints: run `photokeep cache refresh <new-copy>` and `photokeep verify <new-copy>`. If verify fails, ask the owner before deleting the new copy's folder and starting over; the prototype is untouched.
5. Verify and look:
   ```
   photokeep verify <new-copy> --json
   photokeep serve <new-copy>
   ```
   Success: `"ok": true`, and the owner sees their photos in the viewer.
6. Add drives with [add-drive](add-drive.md). Then ask the owner whether to delete the prototype, the raw folder and the zips. Never delete them yourself.

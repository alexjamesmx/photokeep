# Update the app on a copy

Each copy carries its own `app/`. Sync can move the newer one onto the older, so a drive that was offline for a while gets the new viewer. Only run this when the owner asks, or when `app.direction` in a sync plan is not null.

1. Check the versions with the sync plan (this changes nothing):
   ```
   photokeep sync <copy-a> <copy-b> --update-app --json
   ```
   `app` shows `a`, `b` and `direction`. `a->b` means a's app is newer and replaces b's. `null` means equal, or one side has no `app/`: nothing to do.
2. Confirm with the owner that the newer app is the one to keep, and that the sync itself is fine (see [sync](sync.md), including its stop rules).
3. Carry it out:
   ```
   photokeep sync <copy-a> <copy-b> --yes --update-app --json
   ```
   Success: exit 0, the sync success signals from [sync](sync.md), and a fresh plan reports `app.direction` null.
4. Check the viewer starts:
   ```
   photokeep serve <copy> --no-browser
   ```
   Success: it prints `Open http://127.0.0.1:<port>/`. Stop it with Ctrl+C.

The copy's data never depends on `app/`: only the viewer does.

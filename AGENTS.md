# Photokeep

## Goal

Photokeep is a local-first photo archive for people who want to stop depending on cloud providers. The library is a set of equal copies (a laptop folder, an external drive). Each copy holds the same files and a change log per copy, and any two copies sync both ways. Dates, places, camera, original names and provider metadata are never lost or altered. This file is enough to back up, sync, verify, repair, update and extend a library on request.

Phones are planned (as a source of new photos, and as a consumer filled newest-first with a 15% reserve). They are not built; there are no phone commands.

This is the only agent guide. Agents that read `AGENTS.md` natively need nothing else. For one that doesn't, point it here (a setting, or a one-line file saying "Read AGENTS.md") rather than copying text, so the rules live in one place.

## Invariants

- Never modify an original's bytes. Files in `originals/` and `trash/` are only renamed, copied with hash verification, or deleted when their item is purged.
- Never lose or alter metadata: dates, places, camera, original names, and provider data under `sources/`.
- Dry-run before anything destructive and show the summary to the owner.
- Ask the owner before `--yes` on a sync that has deletes or purges, before `repair`, before deleting anything outside a library, and before `purge` or `readmit`.
- Purge is final and there is no resurrection: a purged item is never re-imported unless the owner explicitly asks for `readmit`. `readmit` is an event in the log format with no command yet: tell the owner, never write one by hand.
- Never edit a log under `log/` by hand. Logs only grow, and only Photokeep appends to them.
- Trash is global and lasts 30 days. A purge event is written at the next sync after that.
- Every copy is equal. No copy is the master, and `copy.json` is never synced.
- Never put personal data in the repo (names, emails, home paths, drive names, account names). Run `python3 scripts/check_private.py` before every commit; it must exit 0.
- Commit messages: one plain line, no AI attribution. Never push without the owner's say.

## How copies are recognized

By `copy.json` (`copy_id`) and `library.json` (`library_id`) at the copy's root, never by mount path. A drive can mount anywhere. To find copies, look for folders that hold both files, then run `photokeep status <path>`.

Layout and event format: [docs/library-format.md](docs/library-format.md).

## Commands

Run as `uv run photokeep ...`, or `python3 -m photokeep ...` with `PYTHONPATH=app` inside a copy. Every subcommand accepts `--json`. Exit 0 is success, 1 is an error (message on stderr), 2 means it finished but the owner has to look.

- `photokeep init <path> --name <name> [--copy-of <path>]` creates a library, or a new copy of an existing one. It refuses a folder that is not empty.
- `photokeep sync <a> <b> [--yes] [--update-app]` syncs two copies both ways. Without `--yes` it only prints the plan, which is the dry run. Exit 2 when copies could not be made whole.
- `photokeep status <copy>` shows items, size, known copies and the last event.
- `photokeep verify <copy> [--sample N]` rehashes a copy's files against the log. Exit 2 when something is missing, corrupt or untracked.
- `photokeep repair <copy> --from <copy> [--dry-run]` restores missing and corrupt files from another copy's verified bytes. A corrupt file moves to `quarantine/` only once its replacement is ready. Exit 2 when some stay bad.
- `photokeep cache refresh <copy> [--no-render]` rebuilds the viewer index and renders missing thumbnails. Cache is always rebuildable.
- `photokeep serve <copy> [--port N] [--no-browser]` runs the viewer until Ctrl+C. It records trash, restore, delete and favorite as events.
- `photokeep app install <copy>` puts the running Photokeep in the copy's `app/` and refreshes its launchers. Sync then carries it to copies with an older app (see update-app).
- `photokeep migrate-v0 <old> <copy> --raw <dir> --zip <file>... [--dry-run]` fills a new copy from the prototype library, once. It copies; the prototype is never changed.

## Runbooks

Numbered steps to follow verbatim.

- [docs/runbooks/sync.md](docs/runbooks/sync.md): sync two copies.
- [docs/runbooks/add-drive.md](docs/runbooks/add-drive.md): add a drive as a new copy and fill it.
- [docs/runbooks/verify-repair.md](docs/runbooks/verify-repair.md): check a copy and repair it from another.
- [docs/runbooks/update-app.md](docs/runbooks/update-app.md): move the newer app to the older copy.
- [docs/runbooks/migrate-v0.md](docs/runbooks/migrate-v0.md): one-time import of the prototype library.

Design: [docs/architecture.md](docs/architecture.md). Decisions: [docs/decisions/](docs/decisions/0001-folders-and-logs.md).

## Development

`uv sync`, then `uv run pytest -q` and `uv run mypy photokeep`; CI runs both. Any change under `photokeep/` bumps `__version__` in `photokeep/__init__.py` (the patch number at least), the only place the version lives: sync moves an `app/` only onto a lower version, so an unbumped change never reaches the copies. The suite builds synthetic fixtures; there are no real photos. Core modules use only the standard library; `media.py` imports Pillow and numpy lazily (extra `media`).

Module map (`photokeep/`):

- `cli.py`: argument parsing and the `COMMANDS` registry.
- `library.py`: `Library`, init/open of a copy, the per-copy lock.
- `events.py`: change-log lines: validate, append, read, order.
- `state.py`: folds ordered events into items, trash and blocks.
- `paths.py`: names and folders for each item (`assign`).
- `fsops.py`: scan, plan and apply layout, hash-verified copy.
- `hashcache.py`: size+mtime hash cache in `cache/files.sqlite`.
- `sync.py`: logs, purges, sources, files, optional app, index refresh.
- `sync_sources.py`: the `sources/` part of sync: write-once import folders, files only added.
- `verify.py`: `verify` and `repair`.
- `status.py`: counts for `status`.
- `cache.py`: viewer index `cache/index.sqlite` and thumbnails.
- `media.py`: thumbnails, HEIC previews, similar-photo grouping.
- `metadata.py`: display metadata from EXIF and provider data, never written back.
- `server.py`: local viewer, JSON API, write path to the log.
- `appcopy.py`: the `app/` copy and launchers in every copy.
- `locks.py`: advisory file locks.
- `migrate_v0.py`, `zipscan.py`: the one-time prototype migration and zip hashing.
- `providers/`: readers of provider metadata (`icloud.py`).
- `ui/`: the viewer's HTML, CSS and JS.
- `launchers/`: `Open Photos` scripts copied to each copy's root.

## Extending

- **An event op.** Add its required string fields to `_FIELDS` in `events.py`, its fold rule to `State.apply` in `state.py`, a row in the op table of `docs/library-format.md`, and tests. Older readers keep unknown ops and ignore them, so the format stays compatible. If the viewer writes it, add it to `ACTIONS` and `Viewer._change` in `server.py`.
- **An importer adapter.** Write verbatim provider files to `sources/<import-id>/` with a `manifest.json` (`import`, `adapter`, `exif_root`: the folder exiftool ran on, `files` of `{source_path, sha256, size, mtime, path}` where `path` is the library path at import time, null when the bytes were not imported). List every source file; the first entry of a SHA-256 is the name the item keeps. Put a reader `read(source_dir) -> {source_path: record}` in `providers/<adapter>.py` (see `icloud.py` for the record keys) and register it in `PROVIDERS` in `cache.py`. Write `import` events with the same content hashes, never changing bytes. An importer must skip anything already seen or blocked in the folded state.
- **A UI view.** Add it in `photokeep/ui/` (the page loads `/api/items`; writes are POSTs to the `ACTIONS` routes with the page token). Keep the viewer read-only except through events.

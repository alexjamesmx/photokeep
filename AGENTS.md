# Photokeep

## Goal

Local-first photo archive with equal copies (plain folders plus per-copy change logs), driven by a CLI.

## Invariants

- Never modify an original's bytes.
- Never put personal data in the repo; run `python3 scripts/check_private.py` before every commit.
- Dry-run before anything destructive and show the summary.
- Ask the owner before `purge` or anything outside the library.

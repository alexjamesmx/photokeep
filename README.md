# Photokeep

A local-first photo archive for anyone who wants to stop depending on cloud providers. Your photos live in plain folders, kept as several equal copies (a laptop, an external drive) that sync through per-copy change logs. Original files and their metadata are never modified.

Why: cloud services come and go, change terms and lock your photos in. Folders and a small, documented log format outlast them, and any copy can rebuild the rest.

## Quick start

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```
uv sync
uv run photokeep init ~/Photos --name laptop
uv run photokeep init /path/to/drive/Photos --name drive --copy-of ~/Photos
uv run photokeep sync ~/Photos /path/to/drive/Photos          # shows the plan
uv run photokeep sync ~/Photos /path/to/drive/Photos --yes    # carries it out
uv run photokeep serve ~/Photos                                # opens the viewer
uv run photokeep import takeout takeout-*.zip --into ~/Photos --dry-run   # Google Takeout; needs exiftool
```

Every copy also carries its own `app/` and an `Open Photos` launcher, so a drive opens on any computer with Python.

## More

- [AGENTS.md](AGENTS.md): commands, rules and runbooks. Written for AI agents, readable by people.
- [docs/library-format.md](docs/library-format.md): the on-disk format.
- [docs/architecture.md](docs/architecture.md): how it works.
- [docs/decisions/](docs/decisions/0001-folders-and-logs.md): why it is built this way.

Licence: MIT.

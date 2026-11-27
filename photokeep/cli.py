import argparse
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from photokeep import __version__, sync
from photokeep.events import LogCorrupt
from photokeep.fsops import CopyError
from photokeep.library import LibraryError, init_copy, init_library, open_library

# Later tasks register their subcommand handlers here by name.
COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {}
# Optional per-command argument definitions, keyed like COMMANDS.
ARGUMENTS: dict[str, Callable[[argparse.ArgumentParser], None]] = {}


def _init_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("path", type=Path)
    p.add_argument("--name", required=True, help="name of this copy")
    p.add_argument("--copy-of", type=Path, metavar="PATH", help="join the library at PATH as another copy")


def cmd_init(args: argparse.Namespace) -> int:
    """Create a library, or a new copy of an existing one."""
    try:
        if args.copy_of:
            lib = init_copy(args.path, args.name, of=open_library(args.copy_of))
        else:
            lib = init_library(args.path, args.name)
    except LibraryError as e:
        print(f"photokeep: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"root": str(lib.root), "library_id": lib.library_id, "copy_id": lib.copy_id}))
    else:
        print(f"root: {lib.root}\nlibrary id: {lib.library_id}\ncopy id: {lib.copy_id}")
    return 0


COMMANDS["init"] = cmd_init
ARGUMENTS["init"] = _init_args


def _sync_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("a", type=Path, help="a copy of the library")
    p.add_argument("b", type=Path, help="another copy of the same library")
    p.add_argument("--yes", action="store_true", help="carry out the sync; without it only the plan is shown")
    p.add_argument("--update-app", action="store_true", help="with --yes, also copy the newer app/ over the older")


def _counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{n} {what}" for what, n in counts.items())


def _some(names: list[str], shown: int = 3) -> str:
    """A count, then the first few names; the full list is in --json."""
    if not names:
        return "0"
    return f"{len(names)} ({', '.join(names[:shown])}{', ...' if len(names) > shown else ''})"


def _app_text(app: dict, dry_run: bool, update_app: bool) -> str:
    text, way = f"app: a {app['a'] or 'none'}, b {app['b'] or 'none'}", app["direction"]
    if not way:
        return text
    if not update_app:
        return f"{text}; add --update-app to copy {way}"
    return f"{text}; --yes copies {way}" if dry_run else f"{text}; copied {way}"


def _sync_text(s: dict, dry_run: bool, update_app: bool) -> str:
    bad = [f"{b['side']}:{b['path']}" for b in s["bad_sources"]]
    lines = [
        f"logs: {len(s['logs'])} files, {sum(size for *_, size in s['logs'])} bytes",
        f"files: {s['copy_a_to_b']} a->b, {s['copy_b_to_a']} b->a",
        f"purges: {_some([p['item'][:12] for p in s['new_purges']])}",
        f"layout a: {_counts(s['layout_a'])}",
        f"layout b: {_counts(s['layout_b'])}",
        f"missing on both: {s['missing_both']}",
        f"bad sources: {_some(bad)}",
        _app_text(s["app"], dry_run, update_app),
        f"conflicts: {s['conflicts']}",
    ]
    if s["pending"]:
        lines += [f"pending {side}: {_counts(counts)}" for side, counts in s["pending"].items()]
    if dry_run:
        lines.append("dry run: nothing changed; add --yes to sync")
    return "\n".join(lines)


def cmd_sync(args: argparse.Namespace) -> int:
    """Sync two copies both ways (shows the plan only, unless --yes)."""
    try:
        a, b = open_library(args.a), open_library(args.b)
        now = datetime.now(UTC)
        result = sync.run(a, b, now, update_app=args.update_app) if args.yes else sync.plan(a, b, now)
    except (LibraryError, LogCorrupt, CopyError, OSError) as e:
        print(f"photokeep: {e}", file=sys.stderr)
        return 1
    summary = result.summary()
    print(json.dumps(summary) if args.json else _sync_text(summary, not args.yes, args.update_app))
    # Exit 2: sync finished, but the copies could not be made whole and the owner has to look.
    return 2 if summary["conflicts"] or summary["missing_both"] or summary["bad_sources"] else 0


COMMANDS["sync"] = cmd_sync
ARGUMENTS["sync"] = _sync_args


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="photokeep")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    sub = parser.add_subparsers(dest="command", metavar="command")
    for name, handler in COMMANDS.items():
        doc = (handler.__doc__ or "").strip()
        p = sub.add_parser(name, help=doc.splitlines()[0] if doc else None)
        p.add_argument("--json", action="store_true", help="machine-readable output")
        if name in ARGUMENTS:
            ARGUMENTS[name](p)
        p.set_defaults(handler=handler)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(f"photokeep {__version__}")
        return 0
    if args.command is None:
        parser.print_help()
        return 2
    return args.handler(args)

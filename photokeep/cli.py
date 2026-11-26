import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from photokeep import __version__
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

import argparse
from collections.abc import Callable

from photokeep import __version__

# Later tasks register their subcommand handlers here by name.
COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="photokeep")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    sub = parser.add_subparsers(dest="command", metavar="command")
    for name, handler in COMMANDS.items():
        doc = (handler.__doc__ or "").strip()
        p = sub.add_parser(name, help=doc.splitlines()[0] if doc else None)
        p.add_argument("--json", action="store_true", help="machine-readable output")
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

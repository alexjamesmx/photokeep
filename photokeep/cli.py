import argparse
import json
import sqlite3
import sys
import webbrowser
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from photokeep import __version__, appcopy, media, server, status, sync, verify
from photokeep.cache import Cache
from photokeep.events import LogCorrupt
from photokeep.fsops import CopyError
from photokeep.library import LibraryError, init_copy, init_library, open_library

# Later tasks register their subcommand handlers here by name.
COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {}
# Optional per-command argument definitions, keyed like COMMANDS.
ARGUMENTS: dict[str, Callable[[argparse.ArgumentParser], None]] = {}


def _cache_db_error(e: sqlite3.Error) -> int:
    print(f"photokeep: cache database error ({e}); cache/ holds only rebuildable data and can be deleted",
          file=sys.stderr)
    return 1


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
        appcopy.install(lib)
    except (LibraryError, OSError) as e:
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
        f"sources: {s['sources']['a_to_b']} a->b, {s['sources']['b_to_a']} b->a, "
        f"sources conflicts: {_some(s['sources']['conflicts'])}",
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
    except sqlite3.Error as e:
        return _cache_db_error(e)
    summary = result.summary()
    print(json.dumps(summary) if args.json else _sync_text(summary, not args.yes, args.update_app))
    # Exit 2: sync finished, but the copies could not be made whole and the owner has to look.
    problems = (summary["conflicts"], summary["missing_both"], summary["bad_sources"], summary["sources"]["conflicts"])
    return 2 if any(problems) else 0


COMMANDS["sync"] = cmd_sync
ARGUMENTS["sync"] = _sync_args


def _copy_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("copy", type=Path, help="a copy of the library")


def _verify_args(p: argparse.ArgumentParser) -> None:
    _copy_arg(p)
    p.add_argument("--sample", type=int, metavar="N", help="rehash only N random files")


def _verify_text(r: verify.VerifyReport) -> str:
    lines = [f"checked: {r.checked}", f"missing: {_some(r.missing)}", f"corrupt: {_some(r.corrupt)}",
             f"untracked: {_some(r.untracked)}"]
    return "\n".join(lines + ["ok" if r.ok else "not ok"])


def _report(args: argparse.Namespace, r: verify.VerifyReport) -> int:
    print(json.dumps(r.to_json()) if args.json else _verify_text(r))
    return 0 if r.ok else 2


def cmd_verify(args: argparse.Namespace) -> int:
    """Rehash a copy's files against the log (exit 2 when something is wrong)."""
    try:
        return _report(args, verify.verify(open_library(args.copy), sample=args.sample))
    except (LibraryError, LogCorrupt, OSError) as e:
        print(f"photokeep: {e}", file=sys.stderr)
        return 1
    except sqlite3.Error as e:
        return _cache_db_error(e)


def _repair_args(p: argparse.ArgumentParser) -> None:
    _copy_arg(p)
    p.add_argument("--from", dest="source", type=Path, required=True, metavar="COPY", help="the copy to restore from")


def cmd_repair(args: argparse.Namespace) -> int:
    """Restore a copy's bad or missing files from another copy (exit 2 when some stay bad)."""
    try:
        return _report(args, verify.repair(open_library(args.copy), open_library(args.source)))
    except (LibraryError, LogCorrupt, CopyError, OSError) as e:
        print(f"photokeep: {e}", file=sys.stderr)
        return 1
    except sqlite3.Error as e:
        return _cache_db_error(e)


def cmd_status(args: argparse.Namespace) -> int:
    """Summarize a copy: items, size and known copies."""
    try:
        s = status.status(open_library(args.copy))
    except (LibraryError, LogCorrupt, OSError) as e:
        print(f"photokeep: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(s))
    else:
        names = ", ".join(f"{n} ({c[:8]})" for c, n in s["copies"].items())
        print(f"copy: {s['copy_name']} ({s['copy_id'][:8]}) of library {s['library_id'][:8]}\n"
              f"items: {s['items_live']} live, {s['items_trashed']} trashed; {s['bytes_live']} bytes live\n"
              f"copies: {names}\nlast event: {s['last_event_ts'] or 'none'}")
    return 0


def _cache_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("action", choices=["refresh"])
    _copy_arg(p)
    p.add_argument("--no-render", action="store_true", help="rebuild the index only; render no thumbnails")


def cmd_cache(args: argparse.Namespace) -> int:
    """Rebuild a copy's viewer index and render missing thumbnails (`cache refresh`)."""
    try:
        counts = Cache(open_library(args.copy)).refresh(render=not args.no_render)
    except (LibraryError, LogCorrupt, OSError) as e:
        print(f"photokeep: {e}", file=sys.stderr)
        return 1
    except sqlite3.Error as e:
        return _cache_db_error(e)
    if args.json:
        print(json.dumps(counts))
        return 0
    print("\n".join(f"{what.replace('_', ' ')}: {n}" for what, n in counts.items()))
    missing = media.available()[1]
    if counts["skipped_render"] and not args.no_render and missing:
        print(f"to render thumbnails, install: {', '.join(missing)}")
    return 0


def _port(text: str) -> int:
    if not text.isdigit() or not 1 <= int(text) <= 65535:
        raise argparse.ArgumentTypeError(f"{text!r} is not a port number (1-65535)")
    return int(text)


def _serve_args(p: argparse.ArgumentParser) -> None:
    _copy_arg(p)
    p.add_argument("--port", type=_port, default=8765,
                   help=f"first port to try (default 8765; up to {server.PORTS} are tried)")
    p.add_argument("--no-browser", action="store_true", help="only print the address; open no browser")


def cmd_serve(args: argparse.Namespace) -> int:
    """Open the viewer for a copy (runs until Ctrl+C)."""
    try:
        # absolute: the launchers pass ".", and the page shows the root and hands paths to other apps
        lib = open_library(args.copy.absolute())
        srv = server.make_server(lib, args.port)
    except (LibraryError, LogCorrupt, OSError) as e:
        print(f"photokeep: {e}", file=sys.stderr)
        return 1
    except sqlite3.Error as e:
        return _cache_db_error(e)
    url = f"http://127.0.0.1:{srv.server_port}/"
    text = f"Photos: {lib.root}\nOpen {url}  (Ctrl+C to stop)"
    print(json.dumps({"root": str(lib.root), "url": url}) if args.json else text, flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


COMMANDS["verify"], ARGUMENTS["verify"] = cmd_verify, _verify_args
COMMANDS["repair"], ARGUMENTS["repair"] = cmd_repair, _repair_args
COMMANDS["status"], ARGUMENTS["status"] = cmd_status, _copy_arg
COMMANDS["cache"], ARGUMENTS["cache"] = cmd_cache, _cache_args
COMMANDS["serve"], ARGUMENTS["serve"] = cmd_serve, _serve_args


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

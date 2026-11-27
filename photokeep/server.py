"""Local viewer for one copy: serves the UI, thumbnails and media, and records trash, restore, delete
forever and favorites as events in the copy's log, then moves the files the way sync would.

Listens on 127.0.0.1 only. Write requests need the per-run token embedded in the page, so other
websites open in the same browser cannot delete photos.
"""
import gzip
import json
import os
import secrets
import subprocess
import sys
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

from photokeep import events
from photokeep.cache import Cache, regular
from photokeep.fsops import apply_layout, inside, plan_layout, scan
from photokeep.library import CopyBusy, Library, locked
from photokeep.metadata import VIDEO_EXTS
from photokeep.paths import assign, valid_id
from photokeep.state import State, fold

UI = Path(__file__).with_name("ui")
PORTS = 20  # tried upwards from the requested port
STATIC = {"/app.js": "text/javascript", "/timeline.js": "text/javascript", "/app.css": "text/css"}
MEDIA = ("t", "p", "o", "v", "l")  # thumbnail, preview, original (download), video, live clip
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
               ".gif": "image/gif", ".heic": "image/heic"}
# QuickTime and MP4 share the ISO container; browsers only try to play it when told it is mp4.
VIDEO_TYPE = "video/mp4"
# What "open in another app" opens itself. Anything else may be a program planted on a copy (a .desktop,
# .command or .bat file runs when opened), so only its folder is shown.
OPENABLE = frozenset(IMAGE_TYPES) | VIDEO_EXTS
ACTIONS = {"/api/trash": "trash", "/api/restore": "restore", "/api/delete": "delete", "/api/favorite": "favorite"}
TRASH_REASON = "Deleted in Photos"
MAX_BODY = 8 << 20


class BadRequest(Exception):
    pass


class Viewer:
    """The copy as the viewer shows it: the folded log, where it puts each file, and the index."""

    def __init__(self, lib: Library):
        self.lib = lib
        self.cache = Cache(lib)
        self.lock = threading.Lock()  # request threads share the state, the paths and the index
        self.sizes: dict[str, int] | None = None
        self.state = State()
        self.paths: dict[str, dict[str, str]] = {}
        with self.lock:
            self.catch_up()

    def _log_sizes(self) -> dict[str, int]:
        return {p.name: p.stat().st_size for p in self.lib.log_dir.glob("*.jsonl")}

    def _fold(self) -> dict[str, int]:
        """Re-fold the logs; return their sizes, taken first so a line appended meanwhile is never missed.

        The caller records the sizes once the index has caught up too, so a failure is retried next request.
        """
        sizes = self._log_sizes()
        self.state = fold(events.read_all(self.lib))
        self.paths = assign(self.state)
        return sizes

    def catch_up(self) -> None:
        """Re-fold and re-index if another process (sync, import) has written to the logs; caller holds lock."""
        if self._log_sizes() != self.sizes:
            sizes = self._fold()
            self.cache.refresh(render=False)
            self.sizes = sizes

    def items(self) -> list[dict]:
        with self.lock:
            self.catch_up()
            return self.cache.items_json()

    def roles(self, item_id: str) -> dict[str, str] | None:
        """The item's paths by role (main, live), or None when the log shows no such item."""
        with self.lock:
            self.catch_up()
            return self.paths.get(item_id)

    def file(self, rel: str | None) -> Path | None:
        # Paths come from logs that other drives wrote too: only files inside originals/ and trash/ are served.
        path = self.lib.root / rel if rel else None
        return path if path and inside(self.lib, path) else None

    def _change(self, action: str, item_id: str, value: bool) -> dict | None:
        """The event that makes this change to the item, or None when there is nothing to change."""
        item = self.state.items[item_id] if item_id in self.paths else None
        match action:
            case "trash" if item and item.status == "live":
                return events.new_event(self.lib, "trash", item=item_id, reason=TRASH_REASON)
            case "restore" if item and item.status == "trashed":
                return events.new_event(self.lib, "restore", item=item_id)
            case "delete" if item and item.status == "trashed":
                return events.new_event(self.lib, "purge", item=item_id)
            case "favorite" if item and item.favorite != value:
                return events.new_event(self.lib, "favorite" if value else "unfavorite", item=item_id)
        return None

    def write(self, action: str, ids: list[str], value: bool = False) -> dict:
        """Log the change for each item it changes, move files to match, and return the rows it touched."""
        with self.lock, locked(self.lib):
            self.catch_up()  # under the copy lock, so no other writer appends between this read and ours
            changes = [e for i in dict.fromkeys(ids) if (e := self._change(action, i, value))]
            touched = set(ids)
            if changes:
                before = self.paths
                events.append(self.lib, changes)
                sizes = self._fold()
                # Deleting forever frees a name, so another item can move to it.
                touched |= {i for i in before.keys() | self.paths.keys() if before.get(i) != self.paths.get(i)}
                # The index follows the log, so it is updated before the files: a failed move leaves it right.
                self.cache.refresh_items(touched)
                self.sizes = sizes
            # Favorites move nothing, and scanning every file is most of a write's time. Every other action
            # lays the files out even when it logs nothing new: a retry then finishes a move that failed.
            if action != "favorite":
                failed = apply_layout(self.lib, plan_layout(self.lib, self.state, scan(self.lib)))
                if failed:  # shown in the page; the next request retries the move
                    path, reason = failed[0]
                    raise OSError(f"{len(failed)} files could not be moved, first {path}: {reason}")
            rows = self.cache.items_json(touched)
        return {"items": rows, "gone": sorted(touched - {r["id"] for r in rows})}


def _open_outside(path: Path) -> None:
    """Open a file or folder in the system's own app for it."""
    opener = {"darwin": "open", "win32": "explorer"}.get(sys.platform, "xdg-open")
    subprocess.Popen([opener, str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def _object(raw: bytes) -> dict:
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise BadRequest("the body is not JSON") from None
    if not isinstance(body, dict):
        raise BadRequest("the body must be a JSON object")
    return body


def _content_type(path: Path, kind: str) -> str:
    ext = path.suffix.lower()
    return VIDEO_TYPE if kind == "l" or ext in VIDEO_EXTS else IMAGE_TYPES.get(ext, "application/octet-stream")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: "ViewerServer"

    def log_message(self, format: str, *args: object) -> None:
        pass  # a scrolling grid makes hundreds of requests a second

    def host_ok(self) -> bool:
        # Refuse requests addressed to other host names (DNS rebinding).
        port = self.server.server_port
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def send_bytes(self, body: bytes, ctype: str, status: int = 200, cache: bool = False) -> None:
        if "gzip" in self.headers.get("Accept-Encoding", "") and len(body) > 2048 and not ctype.startswith("image"):
            body = gzip.compress(body, 5)
            self.send_response(status)
            self.send_header("Content-Encoding", "gzip")
        else:
            self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=31536000, immutable" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj: object, status: int = 200) -> None:
        self.send_bytes(json.dumps(obj, separators=(",", ":")).encode(), "application/json", status)

    def send_file(self, path: Path, ctype: str, download_name: str | None = None, cache: bool = False) -> None:
        # Opened before any header goes out, so a missing or unreadable file still gets a clean answer.
        try:
            f = open(path, "rb") if path.is_file() else None
        except OSError:
            f = None
        if f is None:
            return self.send_json({"error": "missing file"}, 404)
        with f:
            self._send_open_file(f, ctype, download_name, cache)

    def _send_open_file(self, f: BinaryIO, ctype: str, download_name: str | None, cache: bool) -> None:
        size = os.fstat(f.fileno()).st_size
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            a, _, b = rng[6:].split(",")[0].strip().partition("-")
            try:
                start, end = (int(a), int(b) if b else size - 1) if a else (max(0, size - int(b)), size - 1)
            except ValueError:
                start, end = 0, size - 1
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end, status = min(end, size - 1), 206
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download_name:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(download_name)}")
        self.send_header("Cache-Control", "max-age=31536000, immutable" if cache else "no-cache")
        self.end_headers()
        try:
            f.seek(start)
            left = end - start + 1
            while left > 0 and (chunk := f.read(min(1 << 20, left))):
                self.wfile.write(chunk)
                left -= len(chunk)
        except OSError:  # the drive went away mid-file, or the browser did: the body stays short
            self.close_connection = True

    def do_GET(self) -> None:
        self.guarded(self.get)

    def do_POST(self) -> None:
        self.guarded(self.post)

    def guarded(self, handle: Callable[[], None]) -> None:
        try:
            if not self.host_ok():
                self.close_connection = True  # a refused request's body is never read
                return self.send_json({"error": "bad host"}, 403)
            handle()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser stopped listening: a thumbnail scrolled away, a video seeked
        except BadRequest as e:
            self.close_connection = True  # what is left of the body would be read as the next request
            self.send_json({"error": str(e)}, 400)
        except CopyBusy as e:  # a sync holds the copy's lock; trying again later works
            self.send_json({"error": str(e)}, 409)
        except Exception as e:  # shown to the owner in the page; the server keeps running
            self.close_connection = True
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    def get(self) -> None:
        path, viewer = self.path.split("?")[0], self.server.viewer
        if path == "/":
            html = (UI / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", self.server.token)
            return self.send_bytes(html.encode(), "text/html; charset=utf-8")
        if path in STATIC:
            return self.send_bytes((UI / path[1:]).read_bytes(), STATIC[path])
        if path == "/api/items":
            return self.send_json({"items": viewer.items(), "root": str(viewer.lib.root)})
        kind, _, item_id = path[1:].partition("/")
        if kind not in MEDIA:
            return self.send_json({"error": "not found"}, 404)
        if not valid_id(item_id):
            raise BadRequest("not an item id")
        self.media(kind, item_id)

    def media(self, kind: str, item_id: str) -> None:
        viewer = self.server.viewer
        roles = viewer.roles(item_id)
        if roles is None:
            return self.send_json({"error": "not found"}, 404)
        thumb, preview = viewer.cache.thumb_path(item_id), viewer.cache.preview_path(item_id)
        # cache/ is not checked like the library's files, so a symlink planted there is never followed
        if kind == "t":
            if not regular(thumb):
                return self.send_json({"error": "not rendered"}, 404)
            return self.send_file(thumb, "image/jpeg", cache=True)
        if kind == "p" and regular(preview):
            return self.send_file(preview, "image/jpeg", cache=True)
        path = viewer.file(roles.get("live" if kind == "l" else "main"))
        if path is None:
            return self.send_json({"error": "not found"}, 404)
        self.send_file(path, _content_type(path, kind), download_name=path.name if kind == "o" else None)

    def body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            raise BadRequest("bad Content-Length") from None
        if not 0 <= length <= MAX_BODY:
            raise BadRequest("the body is too large")
        return self.rfile.read(length)

    def post(self) -> None:
        raw = self.body()  # read first, so the connection stays usable whatever the answer
        if not secrets.compare_digest(self.headers.get("X-Token", "").encode(), self.server.token.encode()):
            return self.send_json({"error": "forbidden"}, 403)
        if self.path == "/api/open":
            return self.open(_object(raw))
        action = ACTIONS.get(self.path)
        if action is None:
            return self.send_json({"error": "not found"}, 404)
        body = _object(raw)
        ids, value = body.get("ids"), body.get("value", False)
        if not isinstance(ids, list) or not all(valid_id(i) for i in ids):
            raise BadRequest("ids must be a list of item ids")
        if not isinstance(value, bool):
            raise BadRequest("value must be true or false")
        self.send_json(self.server.viewer.write(action, ids, value))

    def open(self, body: dict) -> None:
        item_id, viewer = body.get("id"), self.server.viewer
        if not valid_id(item_id):
            raise BadRequest("not an item id")
        roles = viewer.roles(item_id) or {}
        path = viewer.file(roles["live"] if body.get("live") is True and "live" in roles else roles.get("main"))
        if path is None:
            return self.send_json({"error": "not found"}, 404)
        media_file = path.suffix.lower() in OPENABLE and regular(path)
        _open_outside(path if media_file and body.get("folder") is not True else path.parent)
        self.send_json({"ok": True})


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server take a port in use, so the next port would never be tried.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, port: int, viewer: Viewer):
        super().__init__(("127.0.0.1", port), Handler)
        self.viewer = viewer
        self.token = secrets.token_urlsafe(24)


def make_server(lib: Library, port: int) -> ViewerServer:
    """The viewer for lib on 127.0.0.1, at the first free port from `port` up (PORTS are tried)."""
    viewer = Viewer(lib)
    for candidate in range(port, min(port + PORTS, 65536)):
        try:
            return ViewerServer(candidate, viewer)
        except OSError:
            continue
    raise OSError(f"no free port from {port} to {min(port + PORTS, 65536) - 1}")

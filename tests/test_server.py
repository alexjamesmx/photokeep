import gzip
import http.client
import json
import os
import socket
import sqlite3
import threading
from pathlib import Path

import pytest

from photokeep import server
from photokeep.cache import Cache
from photokeep.cli import main
from photokeep.events import append, new_event, read_all
from photokeep.library import locked
from photokeep.paths import assign
from photokeep.server import make_server
from photokeep.state import fold
from tests.helpers import T1, add_item, imp, put, sha


class Client:
    def __init__(self, srv: server.ViewerServer):
        self.port, self.token = srv.server_port, srv.token

    def request(self, method: str, path: str, body: bytes | str | None = None,
                headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            r = conn.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            conn.close()

    def send(self, path: str, body: bytes | str) -> tuple[int, dict[str, str], bytes]:
        return self.request("POST", path, body, {"X-Token": self.token, "Content-Type": "application/json"})

    def get(self, path: str) -> dict:
        status, _, data = self.request("GET", path)
        assert status == 200, data
        return json.loads(data)

    def post(self, path: str, **body) -> dict:
        status, _, data = self.send(path, json.dumps(body))
        assert status == 200, data
        return json.loads(data)


@pytest.fixture
def served(lib, jpeg):
    item = add_item(lib, jpeg)
    srv = make_server(lib, 0)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    yield lib, Client(srv), item
    srv.shutdown()
    srv.server_close()


def last_event(lib) -> dict:
    return read_all(lib)[-1]


def expected_original(lib, item: str) -> Path:
    return lib.root / assign(fold(read_all(lib)))[item]["main"]


def by_id(listing: dict, item: str) -> dict:
    return next(i for i in listing["items"] if i["id"] == item)


def test_trash_then_restore_goes_through_log(served):
    lib, client, item = served
    res = client.post("/api/trash", ids=[item])
    assert last_event(lib)["op"] == "trash" and (lib.root / f"trash/{item}").is_dir()
    assert last_event(lib)["reason"] == "Deleted in Photos"
    assert (by_id(res, item)["tr"], by_id(res, item)["left"], res["gone"]) == (1, 30, [])
    client.post("/api/restore", ids=[item])
    assert last_event(lib)["op"] == "restore" and expected_original(lib, item).exists()
    assert not (lib.trash / item).exists() and by_id(client.get("/api/items"), item)["tr"] == 0


def test_delete_forever_writes_purge_and_removes_file(served):
    lib, client, item = served
    client.post("/api/trash", ids=[item])
    assert client.post("/api/delete", ids=[item]) == {"items": [], "gone": [item]}
    assert last_event(lib)["op"] == "purge"
    assert not any(lib.trash.iterdir()) and not any(lib.originals.iterdir())
    assert client.get("/api/items")["items"] == []


def test_delete_forever_frees_the_name_for_the_next_item(served):
    lib, client, item = served
    other = sha(b"other")
    put(lib, f"originals/2024/01/img_1~{other[:8]}.jpg", b"other")  # imported later, so it took the suffix
    append(lib, [imp(other, name="img_1.jpg", ts=T1, copy=lib.copy_id)])
    client.post("/api/trash", ids=[item])
    res = client.post("/api/delete", ids=[item])
    assert (lib.originals / "2024/01/img_1.jpg").read_bytes() == b"other"
    assert by_id(res, other)["n"] == "img_1.jpg" and res["gone"] == [item]


def test_only_real_changes_are_logged(served):
    lib, client, item = served
    before = len(read_all(lib))
    client.post("/api/restore", ids=[item])  # not in the trash
    client.post("/api/delete", ids=[item])  # deleting forever needs the trash first
    client.post("/api/favorite", ids=[item], value=False)
    assert len(read_all(lib)) == before
    client.post("/api/trash", ids=[item, item])
    client.post("/api/trash", ids=[item])
    assert [e["op"] for e in read_all(lib)[before:]] == ["trash"]


def test_favorite_follows_the_value(served):
    lib, client, item = served
    assert by_id(client.post("/api/favorite", ids=[item], value=True), item)["f"] == 1
    assert last_event(lib)["op"] == "favorite"
    assert by_id(client.post("/api/favorite", ids=[item], value=False), item)["f"] == 0
    assert last_event(lib)["op"] == "unfavorite"


def test_favorite_moves_no_files(served, monkeypatch):
    _, client, item = served

    def no_scan(lib):
        raise AssertionError("a favorite scanned the files")

    monkeypatch.setattr(server, "scan", no_scan)
    assert by_id(client.post("/api/favorite", ids=[item], value=True), item)["f"] == 1


def test_failed_move_is_finished_by_a_retry(served, monkeypatch):
    lib, client, item = served

    def unplugged(lib, plan):
        raise OSError("device disappeared")

    monkeypatch.setattr(server, "apply_layout", unplugged)
    status, _, data = client.send("/api/trash", json.dumps({"ids": [item]}))
    assert status == 500 and "device disappeared" in json.loads(data)["error"]
    assert last_event(lib)["op"] == "trash" and by_id(client.get("/api/items"), item)["tr"] == 1
    monkeypatch.undo()
    client.post("/api/trash", ids=[item])  # nothing left to log, but the move is still owed
    assert (lib.trash / item / "IMG_1.JPG").is_file() and [e["op"] for e in read_all(lib)].count("trash") == 1


def test_a_move_the_disk_refuses_is_shown_and_finished_by_a_retry(served, monkeypatch):
    lib, client, item = served
    real = os.replace

    def refuse(src, dst):
        if os.path.basename(dst) == "IMG_1.JPG":
            raise OSError(22, "Invalid argument", dst)
        real(src, dst)

    monkeypatch.setattr(os, "replace", refuse)
    status, _, data = client.send("/api/trash", json.dumps({"ids": [item]}))
    assert status == 500 and "Invalid argument" in json.loads(data)["error"]
    monkeypatch.undo()
    client.post("/api/trash", ids=[item])
    assert (lib.trash / item / "IMG_1.JPG").is_file()


def test_failed_index_update_is_rebuilt_on_the_next_request(served, monkeypatch):
    lib, client, item = served

    def broken(cache, ids):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(Cache, "refresh_items", broken)
    assert client.send("/api/trash", json.dumps({"ids": [item]}))[0] == 500
    assert by_id(client.get("/api/items"), item)["tr"] == 1
    monkeypatch.undo()
    client.post("/api/trash", ids=[item])
    assert (lib.trash / item / "IMG_1.JPG").is_file()


def test_post_without_token_is_403(served):
    lib, client, item = served
    before = read_all(lib)
    body = json.dumps({"ids": [item]})
    assert client.request("POST", "/api/trash", body)[0] == 403
    assert client.request("POST", "/api/trash", body, {"X-Token": "wrong"})[0] == 403
    assert read_all(lib) == before


def test_foreign_host_is_403(served):
    _, client, item = served
    foreign = {"Host": f"attacker.example:{client.port}"}
    assert client.request("GET", "/api/items", headers=foreign)[0] == 403
    assert client.request("POST", "/api/trash", json.dumps({"ids": [item]}), {**foreign, "X-Token": client.token})[0] == 403
    assert client.request("GET", "/", headers={"Host": f"localhost:{client.port}"})[0] == 200


def test_server_sees_events_written_by_another_process(served):
    lib, client, item = served
    append(lib, [new_event(lib, "favorite", item=item)])  # as if `sync` ran meanwhile
    assert by_id(client.get("/api/items"), item)["f"] == 1


def test_bad_request_bodies_are_400(served):
    lib, client, item = served
    before = read_all(lib)
    bad = [("/api/trash", "not json"), ("/api/trash", "[]"), ("/api/trash", '{"ids": "x"}'),
           ("/api/trash", json.dumps({"ids": ["../../library.json"]})), ("/api/trash", json.dumps({"ids": [1]})),
           ("/api/restore", json.dumps({"ids": [item.upper()]})),
           ("/api/favorite", json.dumps({"ids": [item], "value": "yes"})), ("/api/open", json.dumps({"id": 5}))]
    for path, body in bad:
        assert client.send(path, body)[0] == 400, (path, body)
    assert read_all(lib) == before


def test_write_while_the_copy_is_busy_is_409(served):
    lib, client, item = served
    before = read_all(lib)
    with locked(lib):  # as a running sync holds it
        status, _, data = client.send("/api/trash", json.dumps({"ids": [item]}))
    assert status == 409 and "busy" in json.loads(data)["error"] and read_all(lib) == before


def test_other_library_errors_are_500(served, tmp_path):
    lib, client, _ = served
    index = lib.cache / "index.sqlite"
    index.unlink()
    index.symlink_to(tmp_path / "elsewhere.sqlite")
    status, _, data = client.request("GET", "/api/items")
    assert status == 500 and "symlink" in json.loads(data)["error"]


def test_media_is_served_from_the_logged_path(served, jpeg):
    _, client, item = served
    status, headers, data = client.request("GET", f"/o/{item}")
    assert (status, data, headers["Content-Type"]) == (200, jpeg, "image/jpeg")
    assert headers["Content-Disposition"] == "attachment; filename*=UTF-8''IMG_1.JPG"
    status, headers, data = client.request("GET", f"/o/{item}", headers={"Range": "bytes=0-9"})
    assert (status, data, headers["Content-Range"]) == (206, jpeg[:10], f"bytes 0-9/{len(jpeg)}")
    assert client.request("GET", f"/p/{item}")[2] == jpeg  # no preview rendered: the original
    client.post("/api/trash", ids=[item])
    assert client.request("GET", f"/o/{item}")[2] == jpeg  # followed into the trash


def test_ranges_past_the_end_and_from_the_end(served, jpeg):
    _, client, item = served
    status, headers, data = client.request("GET", f"/o/{item}", headers={"Range": f"bytes={len(jpeg)}-"})
    assert (status, headers["Content-Range"], data) == (416, f"bytes */{len(jpeg)}", b"")
    status, headers, data = client.request("GET", f"/o/{item}", headers={"Range": "bytes=-10"})
    assert (status, data) == (206, jpeg[-10:])
    assert headers["Content-Range"] == f"bytes {len(jpeg) - 10}-{len(jpeg) - 1}/{len(jpeg)}"


def test_download_name_is_percent_encoded(served):
    lib, client, _ = served
    item = add_item(lib, b"cafe", name="Café ñ.JPG")
    headers = client.request("GET", f"/o/{item}")[1]
    assert headers["Content-Disposition"] == "attachment; filename*=UTF-8''Caf%C3%A9%20%C3%B1.JPG"


def test_body_over_the_limit_is_400(served):
    _, client, _ = served
    headers = {"X-Token": client.token, "Content-Length": str(server.MAX_BODY + 1)}
    assert client.request("POST", "/api/trash", b"{}", headers)[0] == 400


def test_missing_or_odd_original_is_a_clean_404(served):
    _, client, item = served
    original = expected_original(served[0], item)
    original.unlink()
    assert client.request("GET", f"/o/{item}")[0] == 404
    original.mkdir()
    assert client.request("GET", f"/o/{item}")[0] == 404


def test_cache_files_that_are_symlinks_are_never_followed(served, tmp_path, jpeg):
    lib, client, item = served
    secret = tmp_path / "secret.jpg"
    secret.write_bytes(b"not for the browser")
    cache = Cache(lib)
    for path in (cache.thumb_path(item), cache.preview_path(item)):
        path.parent.mkdir(parents=True)
        path.symlink_to(secret)
    assert client.request("GET", f"/t/{item}")[0] == 404
    assert client.request("GET", f"/p/{item}")[2] == jpeg  # the original, not the link


@pytest.mark.parametrize("path", ["/o/..%2f..%2flibrary.json", "/o/../../library.json", "/v/" + "0" * 63,
                                  "/t/1", "/o/" + "A" * 64, "/l/" + "0" * 64 + "/x"])
def test_media_ids_are_validated(served, path):
    assert served[1].request("GET", path)[0] == 400


def test_unknown_or_missing_media_is_404(served):
    _, client, item = served
    assert client.request("GET", "/o/" + "0" * 64)[0] == 404
    assert client.request("GET", f"/l/{item}")[0] == 404  # no live clip
    assert client.request("GET", f"/t/{item}")[0] == 404  # not rendered yet
    assert client.request("GET", "/server.py")[0] == 404


def test_page_carries_the_token_and_scripts_are_gzipped(served):
    client = served[1]
    status, _, page = client.request("GET", "/")
    assert status == 200 and f'content="{client.token}"'.encode() in page
    status, headers, data = client.request("GET", "/app.js", headers={"Accept-Encoding": "gzip"})
    assert headers["Content-Encoding"] == "gzip" and gzip.decompress(data) == (server.UI / "app.js").read_bytes()


def test_open_hands_the_logged_path_to_the_system(served, monkeypatch):
    lib, client, item = served
    opened = []
    monkeypatch.setattr(server.subprocess, "Popen", lambda args, **kw: opened.append(args[-1]))
    assert client.post("/api/open", id=item) == {"ok": True}
    client.post("/api/open", id=item, live=True, folder=True)  # no live clip: the photo's folder
    main_file = expected_original(lib, item)
    assert opened == [str(main_file), str(main_file.parent)]
    assert client.send("/api/open", json.dumps({"id": "0" * 64}))[0] == 404


@pytest.mark.parametrize("name", ["evil.desktop", "run.bat", "Open.command", "IMG_9.JPG.sh"])
def test_open_shows_only_the_folder_of_anything_but_photos_and_videos(served, monkeypatch, name):
    lib, client, _ = served
    other = add_item(lib, b"#!/bin/sh\necho planted\n", name=name)
    opened = []
    monkeypatch.setattr(server.subprocess, "Popen", lambda args, **kw: opened.append(args[-1]))
    client.post("/api/open", id=other)
    assert opened == [str(lib.originals / "2024/01")]


def test_open_never_follows_a_symlinked_photo(served, monkeypatch):
    lib, client, item = served
    original = expected_original(lib, item)
    planted = lib.originals / "2024/01/evil.desktop"
    planted.write_bytes(original.read_bytes())
    original.unlink()
    original.symlink_to(planted)
    opened = []
    monkeypatch.setattr(server.subprocess, "Popen", lambda args, **kw: opened.append(args[-1]))
    client.post("/api/open", id=item)
    assert opened == [str(original.parent)]


def test_next_port_is_tried_when_taken(lib):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        srv = make_server(lib, port)
        srv.server_close()
    assert port < srv.server_port < port + server.PORTS


def test_ports_stop_at_65535(lib, monkeypatch):
    tried = []

    def taken(port, viewer):
        if port > 65535:
            raise OverflowError("bind(): port must be 0-65535")
        tried.append(port)
        raise OSError("address in use")

    monkeypatch.setattr(server, "ViewerServer", taken)
    with pytest.raises(OSError, match="65530 to 65535"):
        make_server(lib, 65530)
    assert tried == list(range(65530, 65536))


@pytest.mark.parametrize("port", ["0", "65536", "http"])
def test_cli_serve_refuses_a_bad_port(lib, port, capsys):
    with pytest.raises(SystemExit):
        main(["serve", str(lib.root), "--port", port, "--no-browser"])
    assert "not a port number" in capsys.readouterr().err


def test_cli_serve_refuses_a_folder_that_is_no_library(tmp_path, capsys):
    assert main(["serve", str(tmp_path), "--no-browser"]) == 1
    assert "not a Photokeep library" in capsys.readouterr().err

import uuid

T0 = "2026-01-01T00:00:00.000000Z"
T1 = "2026-01-01T00:00:01.000000Z"
T2 = "2026-01-01T00:00:02.000000Z"
T3 = "2026-01-01T00:00:03.000000Z"
T4 = "2026-01-01T00:00:04.000000Z"


def ev(op: str, item: str | None = None, *, ts: str = T1, copy: str = "c1", **fields) -> dict:
    e = {"v": 1, "id": uuid.uuid4().hex, "ts": ts, "copy": copy, "op": op, **fields}
    if item is not None:
        e["item"] = item
    return e


def imp(item: str, *, name: str = "IMG_1.JPG", folder: str = "2024/01", ts: str = T0,
        copy: str = "c1", fingerprint: list | None = None, live: str | None = None,
        version_of: str | None = None) -> dict:
    files = [{"role": "main", "name": name, "sha256": item, "size": 1, "mtime": ts}]
    if live:
        files.append({"role": "live", "name": live, "sha256": uuid.uuid4().hex, "size": 1, "mtime": ts})
    return ev("import", item, ts=ts, copy=copy, **{"import": uuid.uuid4().hex}, folder=folder,
              files=files, fingerprint=fingerprint, version_of=version_of)

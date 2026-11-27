"""Thumbnails, HEIC previews, quality hints and similar-photo grouping for the viewer's cache.

Pillow and numpy are imported inside functions so the rest of the package stays stdlib-only.
"""
import importlib.util
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

THUMB_SHORT, THUMB_LONG_MAX, PREVIEW_LONG = 400, 1200, 2560
ROTATED = (5, 6, 7, 8)  # EXIF orientations that swap width and height


def available() -> tuple[bool, list[str]]:
    missing = [m for m, mod in (("pillow", "PIL"), ("numpy", "numpy")) if importlib.util.find_spec(mod) is None]
    missing += [t for t in ("heif-dec", "ffmpeg") if shutil.which(t) is None]
    return not missing, missing


def to_rgb(im):
    from PIL import Image

    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.getchannel("A"))
        return bg
    try:
        return im.convert("RGB")
    except (ValueError, OSError):
        return im.convert("L").convert("RGB")


def metrics(thumb) -> dict:
    """Measured on the thumbnail so every photo is judged at the same scale."""
    import numpy as np
    from PIL import Image

    g = np.asarray(thumb.convert("L"), dtype=np.float32)
    lap = g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:] - 4 * g[1:-1, 1:-1]
    # Immerkaer's fast noise estimate.
    n = (g[:-2, :-2] - 2 * g[:-2, 1:-1] + g[:-2, 2:] - 2 * g[1:-1, :-2] + 4 * g[1:-1, 1:-1]
         - 2 * g[1:-1, 2:] + g[2:, :-2] - 2 * g[2:, 1:-1] + g[2:, 2:])
    noise = float(np.abs(n).sum() * np.sqrt(np.pi / 2) / (6 * n.size))
    small = np.asarray(thumb.convert("L").resize((9, 8), Image.LANCZOS), dtype=np.int16)
    # Flat images (black frames, blank screens) all hash alike, so they get no hash.
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    dhash = None if small.std() < 4 else f"{int(''.join('1' if b else '0' for b in bits), 2):016x}"
    return {"sharp": float(lap.var()), "bright": float(g.mean()), "noise": noise, "dhash": dhash}


def _atomic(dst: Path, write) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dst.parent, prefix=f".{dst.name}.", suffix=".tmp")
    os.close(fd)
    try:
        write(tmp)
        os.replace(tmp, dst)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _rel(kind: str, item_id: str, out_dir: Path) -> Path:
    return out_dir / kind / item_id[:2] / f"{item_id}.jpg"


def _save_thumb(im, dst: Path):
    from PIL import Image

    r = min(THUMB_SHORT / min(im.size), THUMB_LONG_MAX / max(im.size), 1.0)
    t = im.resize((max(1, round(im.width * r)), max(1, round(im.height * r))), Image.LANCZOS, reducing_gap=3.0)
    _atomic(dst, lambda tmp: t.save(tmp, "JPEG", quality=80, optimize=True))
    return t


def _render_photo(item_id: str, src: Path, ext: str, out_dir: Path) -> dict:
    from PIL import Image, ImageOps

    res = {}
    with tempfile.TemporaryDirectory() as tmp:
        if ext == ".heic":
            dec = Path(tmp) / "full.jpg"
            subprocess.run(["heif-dec", "-q", "92", str(src), str(dec)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not dec.exists():
                # Files holding several images get numbered outputs; the first is the primary one.
                dec = sorted(Path(tmp).glob("*.jpg"))[0]
            im = Image.open(dec)
            im.load()
            res["w"], res["h"] = im.size
        else:
            im = Image.open(src)
            w, h = im.size
            res["w"], res["h"] = (h, w) if im.getexif().get(0x0112, 1) in ROTATED else (w, h)
            if im.format == "JPEG":
                im.draft("RGB", (PREVIEW_LONG, PREVIEW_LONG))
            im = ImageOps.exif_transpose(im)
        im = to_rgb(im)
        res["preview"] = 0
        if ext == ".heic":
            p = im.copy()
            p.thumbnail((PREVIEW_LONG, PREVIEW_LONG), Image.LANCZOS)
            _atomic(_rel("previews", item_id, out_dir),
                    lambda t: p.save(t, "JPEG", quality=82, optimize=True, progressive=True))
            res["preview"] = 1
        res.update(metrics(_save_thumb(im, _rel("thumbs", item_id, out_dir))))
    res["thumb"] = 1
    return res


def _render_video(item_id: str, src: Path, duration: float | None, out_dir: Path) -> dict:
    scale = "scale='if(gt(iw,ih),-2,400)':'if(gt(iw,ih),400,-2)'"

    def poster(at: float):
        def write(tmp):
            r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{at:.2f}", "-i", str(src),
                                "-frames:v", "1", "-vf", scale, "-q:v", "4", "-y", "-f", "image2", tmp],
                               capture_output=True)
            if r.returncode or not os.path.getsize(tmp):
                raise RuntimeError(r.stderr.decode(errors="replace")[-300:] or "empty poster")
        return write

    for at in (min(1.0, (duration or 0) * 0.3), 0):
        try:
            _atomic(_rel("thumbs", item_id, out_dir), poster(at))
            return {"thumb": 1}
        except RuntimeError as e:
            err = e
    raise err


def render(item_id: str, path: Path, kind: str, ext: str, duration: float | None, out_dir: Path) -> dict:
    """Never raises: one unreadable file must not stop a batch; it shows without a thumbnail."""
    try:
        if kind == "video":
            return _render_video(item_id, path, duration, out_dir)
        from PIL import Image

        # The owner's own camera files; very large panoramas are expected, not decompression bombs.
        Image.MAX_IMAGE_PIXELS = 400_000_000
        return _render_photo(item_id, path, ext, out_dir)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"[:300]}


def _stamp(taken: str | None) -> float:
    # Wall-clock strings carry no zone; reading them all as UTC keeps differences exact.
    return datetime.fromisoformat(taken).replace(tzinfo=timezone.utc).timestamp() if taken else float("nan")


def similar_groups(rows: list[dict]) -> dict[str, tuple[str, int]]:
    """Group photos whose dHash differs by at most 4 bits, or 10 bits when taken within 2 minutes (bursts).
    Screenshots only match near-identically, since app screens share layouts."""
    import numpy as np

    cand = [r for r in rows if r["kind"] == "photo" and r.get("dhash")]
    n = len(cand)
    if n < 2:
        return {}
    h = np.array([int(r["dhash"], 16) for r in cand], dtype=np.uint64)
    t = np.array([_stamp(r["taken"]) for r in cand])
    shot = np.array([bool(r["screenshot"]) for r in cand])
    pop = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a in range(0, n, 512):
        x = h[a:a + 512, None] ^ h[None, :]
        d = pop[x.view(np.uint8)].reshape(x.shape[0], n, 8).sum(-1)
        with np.errstate(invalid="ignore"):
            near = np.abs(t[a:a + 512, None] - t[None, :]) <= 120
        both_shots = shot[a:a + 512, None] & shot[None, :]
        match = np.where(both_shots, d <= 2, (d <= 4) | ((d <= 10) & near))
        for i, j in zip(*np.nonzero(match)):
            i += a
            if j > i:
                parent[find(i)] = find(j)
    groups: dict[int, list[dict]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(cand[i])
    out = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        gid = min(m["id"] for m in members)
        best = max(members, key=lambda m: (m["favorite"], (m.get("w") or 0) * (m.get("h") or 0),
                                           m.get("sharp") or 0, m["size"]))
        for m in members:
            out[m["id"]] = (gid, int(m is best))
    return out

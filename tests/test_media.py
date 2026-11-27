import shutil
import subprocess

import pytest

from photokeep import media

PIL = pytest.importorskip("PIL.Image")
from PIL import Image, ImageFilter  # noqa: E402


def scene(size=(2000, 1500)):
    """Blocky shapes give edges for sharpness and a non-flat dHash."""
    im = Image.new("RGB", size, (30, 30, 30))
    w, h = size
    for i in range(8):
        for j in range(6):
            c = ((i * 37 + j * 11) % 256, (i * 19 + j * 53) % 256, (i * 7 + j * 29) % 256)
            im.paste(c, (i * w // 8, j * h // 6, (i + 1) * w // 8 - 6, (j + 1) * h // 6 - 6))
    return im


def row(id, dhash, **kw):
    return {"id": id, "kind": "photo", "dhash": dhash, "taken": None, "screenshot": False,
            "favorite": False, "w": 100, "h": 100, "sharp": 1.0, "size": 1, **kw}


def test_available_reports_without_raising():
    ok, missing = media.available()
    assert isinstance(ok, bool) and isinstance(missing, list)
    assert ok == (not missing)


def test_render_jpeg_thumb_and_blur(tmp_path):
    src = tmp_path / "a.jpg"
    scene().save(src, "JPEG")
    blurred = tmp_path / "b.jpg"
    scene().filter(ImageFilter.GaussianBlur(6)).save(blurred, "JPEG")
    out = tmp_path / "out"
    ida, idb = "ab" + "0" * 62, "cd" + "1" * 62
    ra = media.render(ida, src, "photo", ".jpg", None, out)
    rb = media.render(idb, blurred, "photo", ".jpg", None, out)
    thumb = out / "thumbs" / "ab" / f"{ida}.jpg"
    assert Image.open(thumb).size == (533, 400)
    assert (ra["w"], ra["h"], ra["thumb"], ra["preview"]) == (2000, 1500, 1, 0)
    assert rb["sharp"] < ra["sharp"]
    assert not list(out.rglob("*.tmp*")) and not (out / "previews").exists()


def test_render_swaps_dimensions_for_exif_rotation(tmp_path):
    src = tmp_path / "r.jpg"
    im = scene((600, 300))
    exif = Image.Exif()
    exif[0x0112] = 6
    im.save(src, "JPEG", exif=exif)
    r = media.render("ee" + "0" * 62, src, "photo", ".jpg", None, tmp_path / "o")
    assert (r["w"], r["h"]) == (300, 600)


def test_render_error_never_raises(tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    r = media.render("ff" + "0" * 62, bad, "photo", ".jpg", None, tmp_path / "o")
    assert set(r) == {"error"} and r["error"].startswith("UnidentifiedImageError")


def test_black_image_has_no_dhash(tmp_path):
    src = tmp_path / "k.jpg"
    Image.new("RGB", (800, 600)).save(src, "JPEG")
    r = media.render("00" + "0" * 62, src, "photo", ".jpg", None, tmp_path / "o")
    assert r["dhash"] is None


def test_near_identical_photos_group_with_larger_best(tmp_path):
    big, small = tmp_path / "big.jpg", tmp_path / "small.jpg"
    scene().save(big, "JPEG", quality=92)
    scene((1000, 750)).save(small, "JPEG", quality=70)
    out = tmp_path / "o"
    rows = []
    for id, p, t, size in (("b" * 64, big, "2024-05-01T10:00:00", 2), ("a" * 64, small, "2024-05-01T10:00:40", 1)):
        r = media.render(id, p, "photo", ".jpg", None, out)
        rows.append(row(id, r["dhash"], taken=t, w=r["w"], h=r["h"], sharp=r["sharp"], size=size))
    assert media.similar_groups(rows) == {"a" * 64: ("a" * 64, 0), "b" * 64: ("a" * 64, 1)}


def test_similar_groups_thresholds():
    hi = 0xFFF << 48
    rows = [
        row("a", f"{0:016x}"), row("b", f"{0b1111:016x}"),                         # 4 bits: always
        row("c", f"{hi | 0x3FF:016x}", taken="2024-01-01T10:00:00"),
        row("d", f"{hi:016x}", taken="2024-01-01T10:01:00"),                       # 10 bits from c, 60 s
        row("e", f"{hi | 0xFFC00:016x}", taken="2024-01-01T12:00:00"),             # 10 bits from d, 2 h
        row("f", None), row("g", f"{0:016x}", kind="video"),
    ]
    got = media.similar_groups(rows)
    assert got["a"][0] == got["b"][0] == "a"
    assert got["c"][0] == got["d"][0] == "c"
    assert "e" not in got and "f" not in got and "g" not in got


def test_screenshots_need_two_bits_and_best_prefers_favorite():
    rows = [row("a", f"{0:016x}", screenshot=True), row("b", f"{0b11100:016x}", screenshot=True),
            row("c", f"{0b11:016x}", screenshot=True, favorite=True)]
    got = media.similar_groups(rows)
    assert got == {"a": ("a", 0), "c": ("a", 1)}  # b is 3 bits away from a


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
def test_render_video_poster(tmp_path):
    src = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    out = tmp_path / "o"
    r = media.render("12" + "0" * 62, src, "video", ".mp4", 2.0, out)
    assert r == {"thumb": 1}
    assert (out / "thumbs" / "12" / ("12" + "0" * 62 + ".jpg")).stat().st_size > 0


@pytest.mark.skipif(not (shutil.which("heif-enc") and shutil.which("heif-dec")), reason="libheif tools missing")
def test_render_heic_writes_preview(tmp_path):
    jpg, heic = tmp_path / "s.jpg", tmp_path / "s.heic"
    scene((3200, 2400)).save(jpg, "JPEG", quality=95)
    subprocess.run(["heif-enc", str(jpg), "-o", str(heic)], check=True, capture_output=True)
    out = tmp_path / "o"
    iid = "34" + "0" * 62
    r = media.render(iid, heic, "photo", ".heic", None, out)
    assert r["preview"] == 1 and (r["w"], r["h"]) == (3200, 2400)
    assert max(Image.open(out / "previews" / "34" / f"{iid}.jpg").size) == 2560

"""The folder adapter: any folder tree's photos and videos, dated by the files themselves."""
import os
import shutil
import sys
from datetime import UTC, datetime

import pytest

from photokeep import importer
from photokeep.adapters import folder
from photokeep.cache import PROVIDERS, Cache
from photokeep.importer import ImportFailed
from tests.helpers import jpeg_bytes

needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool is not installed")
MARCH = datetime(2024, 3, 5, 16, 0, tzinfo=UTC).timestamp()


def write(path, data: bytes, mtime: float | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_lists_photos_and_videos_with_their_dates(tmp_path):
    src, outside = tmp_path / "Camera", tmp_path / "outside"
    photo = jpeg_bytes()
    write(src / "2024/IMG_1.JPG", photo, MARCH)
    write(src / "2024/IMG_1.MOV", b"live clip")
    write(src / "2024/clip.mp4", b"a video")
    write(src / "notes.txt", b"not media")
    write(src / ".DS_Store", b"junk")
    write(src / "2024/._IMG_1.JPG", b"AppleDouble")
    write(outside / "IMG_9.jpg", b"elsewhere")
    if sys.platform != "win32":  # creating symlinks needs privileges there
        (src / "link.jpg").symlink_to(src / "2024/IMG_1.JPG")
        (src / "linked").symlink_to(outside, target_is_directory=True)
    cands, sources = folder.list([src])
    assert sources == []
    assert [c.source_path for c in cands] == ["Camera/2024/IMG_1.JPG", "Camera/2024/IMG_1.MOV", "Camera/2024/clip.mp4"]
    first, clip, video = cands
    assert (first.name, first.size, first.mtime, first.provider) == ("IMG_1.JPG", len(photo), "2024-03-05T16:00:00Z",
                                                                     {})
    assert (clip.live_of, video.live_of, first.live_of) == ("Camera/2024/IMG_1.JPG", None, None)
    with first.read() as f:
        assert f.read() == photo


def test_several_folders_keep_their_names_apart(tmp_path):
    write(tmp_path / "a/IMG_1.jpg", b"one")
    write(tmp_path / "b/IMG_1.jpg", b"two")
    cands, _ = folder.list([tmp_path / "a", tmp_path / "b"])
    assert [c.source_path for c in cands] == ["a/IMG_1.jpg", "b/IMG_1.jpg"]


def test_refuses_what_it_cannot_list(tmp_path):
    write(tmp_path / "IMG_1.jpg", b"one")
    with pytest.raises(ImportFailed, match="is not a folder"):
        folder.list([tmp_path / "IMG_1.jpg"])
    write(tmp_path / "x/a/IMG_1.jpg", b"one")
    write(tmp_path / "y/a/IMG_1.jpg", b"two")
    with pytest.raises(ImportFailed, match="a/IMG_1.jpg is in two sources"):
        folder.list([tmp_path / "x/a", tmp_path / "y/a"])


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs a folder its owner cannot read")
def test_an_unreadable_folder_stops_the_listing(tmp_path):
    locked = write(tmp_path / "src/locked/IMG_1.jpg", b"one").parent
    locked.chmod(0)
    try:
        with pytest.raises(PermissionError):
            folder.list([tmp_path / "src"])
    finally:
        locked.chmod(0o755)


def test_registers_no_provider_reader():
    assert "folder" not in PROVIDERS


@needs_exiftool
def test_folder_import_dates_a_photo_by_its_file(lib, tmp_path, mexico):
    src = tmp_path / "Camera"
    photo = jpeg_bytes()
    write(src / "IMG_1.JPG", photo, MARCH)
    write(src / "IMG_1.MOV", b"live clip")
    cands, sources = folder.list([src])
    got = importer.run(lib, "folder", cands, sources, False, datetime.now(UTC), roots=[src])
    assert (got["imported"], got["live_clips"], got["problems"]) == (1, 1, [])
    assert (lib.originals / "2024/03/IMG_1.JPG").read_bytes() == photo
    (row,) = Cache(lib).items_json()
    assert (row["t"], row["ds"], row["lv"]) == ("2024-03-05T10:00:00", "mtime", 1)

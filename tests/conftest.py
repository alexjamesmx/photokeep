import io

import pytest

from photokeep.library import init_copy, init_library


@pytest.fixture
def lib(tmp_path):
    return init_library(tmp_path / "L", "laptop")


@pytest.fixture
def a(tmp_path):
    return init_library(tmp_path / "A", "laptop")


@pytest.fixture
def b(tmp_path, a):
    return init_copy(tmp_path / "B", "drive", of=a)


@pytest.fixture
def jpeg() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 80, 40)).save(buf, "JPEG")
    return buf.getvalue()

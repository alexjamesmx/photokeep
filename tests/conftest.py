import io

import pytest

from photokeep.library import init_library


@pytest.fixture
def lib(tmp_path):
    return init_library(tmp_path / "L", "laptop")


@pytest.fixture
def jpeg() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 80, 40)).save(buf, "JPEG")
    return buf.getvalue()

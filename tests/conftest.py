import sys
import time

import pytest

from photokeep.library import init_copy, init_library
from tests.helpers import jpeg_bytes


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
    return jpeg_bytes()


@pytest.fixture
def mexico(monkeypatch):
    """Local zone America/Mexico_City (UTC-6, no DST since 2022), for dates shown in local time."""
    if sys.platform == "win32":
        pytest.skip("time.tzset is not available on Windows")
    monkeypatch.setenv("TZ", "America/Mexico_City")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()

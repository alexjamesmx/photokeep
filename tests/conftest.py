import pytest

from photokeep.library import init_library


@pytest.fixture
def lib(tmp_path):
    return init_library(tmp_path / "L", "laptop")

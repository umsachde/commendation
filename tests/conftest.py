import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Point the library cache at a per-test temp file.

    Without this, tests would read and write the real cache in the developer's
    home directory -- polluting it, and letting one test's cache silently
    satisfy another test's library lookup.
    """
    monkeypatch.setattr(server, "CACHE_PATH", tmp_path / "library_cache.json")
    return tmp_path / "library_cache.json"

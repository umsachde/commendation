import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
import store  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Point every persistent store at a per-test temp path.

    Without this, tests would read and write the developer's real library cache
    and mood store -- polluting them, and letting one test's data silently
    satisfy another test's lookup.
    """
    monkeypatch.setattr(server, "CACHE_PATH", tmp_path / "library_cache.json")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "store.db")
    # server caches its connection across calls; drop it so it reopens here.
    monkeypatch.setattr(server, "_store_conn", None)
    return tmp_path


@pytest.fixture
def isolated_cache(isolated_state):
    return isolated_state / "library_cache.json"


@pytest.fixture
def db(isolated_state):
    """An isolated SQLite store, never the developer's real one."""
    conn = store.connect(isolated_state / "store.db")
    yield conn
    conn.close()

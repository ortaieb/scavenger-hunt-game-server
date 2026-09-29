from collections.abc import Iterator
from pathlib import Path

import pytest

from game_server.config import get_settings
from game_server.proximity import hint_rate_limiter
from game_server.referee import build_referee
from game_server.submissions import open_submission_store


@pytest.fixture(autouse=True)
def isolated_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[None]:
    """Isolate every test from the developer's machine, and reset process caches.

    Settings read `.env` from the working directory, and a developer's `.env` may hold a
    real API key: run each test from its own temp dir so none can read it (and so none
    ever calls the real API). Only tests marked `live` keep the real environment.
    """
    if request.node.get_closest_marker("live") is None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GAME_SERVER_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GAME_SERVER_DB_PATH", str(tmp_path / "default.sqlite3"))
    monkeypatch.setenv("GAME_SERVER_IMAGE_BASE_PATH", str(tmp_path / "default-images"))
    caches = (get_settings, open_submission_store, hint_rate_limiter, build_referee)
    for cache in caches:
        cache.cache_clear()
    yield
    for cache in caches:
        cache.cache_clear()

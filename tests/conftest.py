from collections.abc import Iterator
from pathlib import Path

import pytest

from game_server.config import get_settings
from game_server.submissions import open_submission_store


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep every test's database and images in its own temp dir, never in the repo."""
    monkeypatch.setenv("GAME_SERVER_DB_PATH", str(tmp_path / "default.sqlite3"))
    monkeypatch.setenv("GAME_SERVER_IMAGE_BASE_PATH", str(tmp_path / "default-images"))
    get_settings.cache_clear()
    open_submission_store.cache_clear()
    yield
    get_settings.cache_clear()
    open_submission_store.cache_clear()

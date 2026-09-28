from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from game_server.app import create_app
from game_server.config import get_settings
from game_server.sessions import SessionsFileError, load_session_repository


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as test_client:
        yield test_client


def test_root_returns_hello_world(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.text == "Hello, World!"


def test_root_is_plain_text(client: TestClient) -> None:
    response = client.get("/")

    assert response.headers["content-type"].startswith("text/plain")


def test_unknown_path_returns_404(client: TestClient) -> None:
    response = client.get("/does-not-exist")

    assert response.status_code == 404


@pytest.fixture
def fresh_settings() -> Iterator[None]:
    """Make create_app re-read settings and the sessions file from the environment."""
    get_settings.cache_clear()
    load_session_repository.cache_clear()
    yield
    get_settings.cache_clear()
    load_session_repository.cache_clear()


@pytest.mark.usefixtures("fresh_settings")
def test_starts_without_sessions_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GAME_SERVER_SESSIONS_FILE", raising=False)

    with TestClient(create_app()) as test_client:
        assert test_client.get("/").status_code == 200


@pytest.mark.usefixtures("fresh_settings")
def test_startup_fails_on_invalid_sessions_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions_file = tmp_path / "sessions.json"
    sessions_file.write_text('[{"id": "not-a-uuid"}]')
    monkeypatch.setenv("GAME_SERVER_SESSIONS_FILE", str(sessions_file))

    with pytest.raises(SessionsFileError, match="invalid sessions file"):
        create_app()

import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pytest_mock import MockerFixture

from game_server.app import create_app
from game_server.config import Settings, get_settings
from game_server.submissions import SubmissionStore


def client_for(db_path: Path) -> Iterator[TestClient]:
    app = create_app()
    settings = Settings(db_path=db_path)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    yield from client_for(tmp_path / "game.sqlite3")


def test_healthy_when_the_database_answers(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_unhealthy_when_the_database_cannot_be_opened(tmp_path: Path) -> None:
    unopenable = tmp_path / "game.sqlite3"
    unopenable.mkdir()  # a directory where the database file should be

    for test_client in client_for(unopenable):
        response = test_client.get("/health")

        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}


def test_unhealthy_when_a_query_fails(
    client: TestClient, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    mocker.patch.object(SubmissionStore, "ping", side_effect=sqlite3.OperationalError("disk I/O"))
    caplog.set_level(logging.WARNING, logger="game_server")

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert "health check failed: database unavailable (OperationalError)" in caplog.text
    assert "disk I/O" not in response.text


def test_health_reveals_nothing_else(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()["components"]["schemas"]["HealthStatus"]

    assert set(client.get("/health").json()) == {"status"}
    assert set(schema["properties"]) == {"status"}


def test_ping_checks_the_schema(tmp_path: Path) -> None:
    store = SubmissionStore(tmp_path / "game.sqlite3")
    store.ping()  # a fresh, migrated database answers

    with sqlite3.connect(store.db_path) as conn:
        conn.execute("DROP TABLE submissions")
    conn.close()

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        store.ping()

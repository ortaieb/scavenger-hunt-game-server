import logging
from collections.abc import Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import DictRow
from pytest_mock import MockerFixture

from game_server.app import create_app
from game_server.config import Settings, get_settings
from game_server.submissions import SubmissionStore


def client_for(settings: Settings) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield from client_for(Settings())


def test_healthy_when_the_database_answers(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_unhealthy_when_the_database_cannot_be_reached(
    mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    mocker.patch("game_server.submissions.PING_TIMEOUT_SECONDS", 0.5)
    caplog.set_level(logging.WARNING, logger="game_server")
    # Nothing listens on port 1: every connection attempt is refused.
    unreachable = Settings(db_host="127.0.0.1", db_port=1, db_sslmode="disable")

    for test_client in client_for(unreachable):
        response = test_client.get("/health")

        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}
    assert "health check failed: database unavailable (PoolTimeout)" in caplog.text


def test_unhealthy_when_a_query_fails(
    client: TestClient, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    mocker.patch.object(
        SubmissionStore, "ping", side_effect=psycopg.OperationalError("server closed")
    )
    caplog.set_level(logging.WARNING, logger="game_server")

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert "health check failed: database unavailable (OperationalError)" in caplog.text
    assert "server closed" not in response.text


def test_health_reveals_nothing_else(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()["components"]["schemas"]["HealthStatus"]

    assert set(client.get("/health").json()) == {"status"}
    assert set(schema["properties"]) == {"status"}


def test_ping_checks_the_schema(store: SubmissionStore, db: psycopg.Connection[DictRow]) -> None:
    store.ping()  # a freshly created schema answers

    db.execute("ALTER TABLE submissions RENAME TO renamed_submissions")
    try:
        with pytest.raises(psycopg.errors.UndefinedTable):
            store.ping()
    finally:
        db.execute("ALTER TABLE renamed_submissions RENAME TO submissions")

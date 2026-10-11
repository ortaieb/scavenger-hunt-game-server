import logging
from collections.abc import Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient
from migration_files import newest_version
from psycopg.rows import DictRow
from pytest_mock import MockerFixture

from game_server.app import create_app
from game_server.config import Settings, get_settings
from game_server.schema_version import SchemaBehindError
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


def test_unhealthy_when_the_database_is_behind_the_build(
    client: TestClient, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    mocker.patch("game_server.submissions.SCHEMA_VERSION", newest_version() + 1)
    caplog.set_level(logging.WARNING, logger="game_server")

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    needed = f"V{newest_version() + 1}"
    assert f"database schema at V{newest_version()}, this build needs {needed}" in caplog.text


def test_healthy_when_the_database_is_ahead_of_the_build(
    client: TestClient, mocker: MockerFixture
) -> None:
    # A rollback: older code on a newer, backward-compatible schema.
    mocker.patch("game_server.submissions.SCHEMA_VERSION", newest_version() - 1)

    assert client.get("/health").status_code == 200


def test_unhealthy_without_flyway_history(
    client: TestClient, db: psycopg.Connection[DictRow], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="game_server")
    db.execute("ALTER TABLE flyway_schema_history RENAME TO renamed_history")
    try:
        response = client.get("/health")
    finally:
        db.execute("ALTER TABLE renamed_history RENAME TO flyway_schema_history")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert "health check failed: database unavailable (UndefinedTable)" in caplog.text


def test_ping_ignores_failed_migrations(
    store: SubmissionStore, db: psycopg.Connection[DictRow], mocker: MockerFixture
) -> None:
    # A build that needs the next version, whose migration failed: still behind.
    mocker.patch("game_server.submissions.SCHEMA_VERSION", newest_version() + 1)
    newer = str(newest_version() + 1)
    db.execute(
        "INSERT INTO flyway_schema_history (installed_rank, version, description, type, script,"
        " installed_by, execution_time, success)"
        " SELECT max(installed_rank) + 1, %s, 'failed', 'SQL', 'V_failed.sql', 'test', 0, false"
        " FROM flyway_schema_history",
        (newer,),
    )
    try:
        with pytest.raises(SchemaBehindError):
            store.ping()
    finally:
        db.execute("DELETE FROM flyway_schema_history WHERE version = %s", (newer,))

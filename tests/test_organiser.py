"""require_organiser and the organiser key setting."""

import logging
import secrets
from collections.abc import Iterator

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import ValidationError
from pytest_mock import MockerFixture

from game_server import errors
from game_server.config import Settings, get_settings
from game_server.organiser import UNAUTHORISED_BODY, require_organiser

KEY = "SENTINEL-ORGANISER-KEY-7c1e9a"  # 29 characters


def app_with(settings: Settings) -> FastAPI:
    app = FastAPI()
    errors.install(app)
    app.dependency_overrides[get_settings] = lambda: settings

    @app.get("/organise", dependencies=[Depends(require_organiser)])
    def organise() -> dict[str, bool]:
        return {"ok": True}

    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app_with(Settings(organiser_key=KEY))) as test_client:
        yield test_client


def call(client: TestClient, authorization: str | None) -> Response:
    headers = {} if authorization is None else {"Authorization": authorization}
    return client.get("/organise", headers=headers)


def assert_unauthorised(response: Response) -> None:
    assert response.status_code == 401
    assert response.json() == UNAUTHORISED_BODY
    assert response.json() == {"detail": "organiser key required", "code": "organiser_unauthorised"}
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "authorization",
    [f"Bearer {KEY}", f"bearer {KEY}", f"BEARER   {KEY}  "],
    ids=["exact", "lower-case-scheme", "surrounding-spaces"],
)
def test_the_key_is_accepted(client: TestClient, authorization: str) -> None:
    assert call(client, authorization).json() == {"ok": True}


@pytest.mark.parametrize(
    "authorization",
    [
        pytest.param(None, id="no-header"),
        pytest.param("", id="empty-header"),
        pytest.param(f"Basic {KEY}", id="other-scheme"),
        pytest.param(KEY, id="no-scheme"),
        pytest.param("Bearer", id="bearer-without-key"),
        pytest.param("Bearer wrong-key-but-long-enough-000", id="wrong-key"),
        pytest.param(f"Bearer {KEY.lower()}", id="wrong-case"),
        pytest.param(f"Bearer {KEY}X", id="key-with-extra"),
    ],
)
def test_anything_else_is_401(client: TestClient, authorization: str | None) -> None:
    assert_unauthorised(call(client, authorization))


@pytest.mark.parametrize("authorization", [None, f"Bearer {KEY}"], ids=["no-header", "any-key"])
def test_without_a_key_configured_everything_is_401(authorization: str | None) -> None:
    with TestClient(app_with(Settings())) as test_client:
        assert_unauthorised(call(test_client, authorization))


def test_keys_are_compared_in_constant_time(client: TestClient, mocker: MockerFixture) -> None:
    compare = mocker.spy(secrets, "compare_digest")

    call(client, f"Bearer {KEY}")

    compare.assert_called_once_with(KEY.encode(), KEY.encode())


def test_the_key_is_never_in_a_response_or_log(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    responses = [call(client, f"Bearer {KEY}"), call(client, f"Bearer {KEY}-WRONG")]

    for response in responses:
        assert KEY not in response.text
    assert KEY not in caplog.text


# --- the setting ----------------------------------------------------------------------------


def test_a_short_key_stops_startup_without_echoing_it() -> None:
    short = "SENTINEL-SHORT-KEY-123"  # 22 characters

    with pytest.raises(ValidationError) as raised:
        Settings(organiser_key=short)

    assert "at least 24 characters" in str(raised.value)
    assert short not in str(raised.value)


@pytest.mark.parametrize("key", ["x" * 24, KEY], ids=["exactly-24", "longer"])
def test_a_long_enough_key_is_accepted(key: str) -> None:
    settings = Settings(organiser_key=key)

    assert settings.organiser_key is not None
    assert settings.organiser_key.get_secret_value() == key
    assert key not in repr(settings)


def test_no_key_by_default() -> None:
    assert Settings().organiser_key is None


def test_settings_errors_never_echo_a_value() -> None:
    db_url = "postgresql://user:SENTINEL-PASSWORD@host/db"

    with pytest.raises(ValidationError) as raised:
        # A model-level error: without hiding, it would show every input, the URL too.
        Settings(db_url=db_url, db_pool_min_size=5, db_pool_max_size=2)

    assert "SENTINEL-PASSWORD" not in str(raised.value)

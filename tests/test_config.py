from pathlib import Path

import pytest
from pydantic import ValidationError

from game_server.config import Settings, get_settings


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run each test in an empty directory with no GAME_SERVER_* variables set."""
    monkeypatch.chdir(tmp_path)
    for name in (
        "GAME_SERVER_HOST",
        "GAME_SERVER_PORT",
        "GAME_SERVER_LOG_LEVEL",
        "GAME_SERVER_IMAGE_BASE_PATH",
        "GAME_SERVER_MAX_IMAGE_BYTES",
        "GAME_SERVER_SESSIONS_FILE",
        "GAME_SERVER_DB_PATH",
        "GAME_SERVER_MAX_CAPTURE_AGE_SECONDS",
        "GAME_SERVER_MAX_CLOCK_SKEW_SECONDS",
        "GAME_SERVER_PHASH_MAX_DISTANCE",
        "GAME_SERVER_PROXIMITY_HINT_INTERVAL_SECONDS",
        "GAME_SERVER_ANTHROPIC_API_KEY",
        "GAME_SERVER_REFEREE_MODEL",
        "GAME_SERVER_REFEREE_TIMEOUT_SECONDS",
        "GAME_SERVER_REFEREE_MAX_RETRIES",
        "GAME_SERVER_REFEREE_MAX_IMAGE_EDGE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_defaults() -> None:
    settings = Settings()

    assert settings.host == "0.0.0.0"
    assert settings.port == 8000
    assert settings.log_level == "info"


def test_env_var_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAME_SERVER_PORT", "9001")

    assert Settings().port == 9001


def test_dotenv_file_overrides_default(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("GAME_SERVER_PORT=9100\nGAME_SERVER_LOG_LEVEL=debug\n")

    settings = Settings()

    assert settings.port == 9100
    assert settings.log_level == "debug"


def test_env_var_takes_precedence_over_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("GAME_SERVER_PORT=9100\n")
    monkeypatch.setenv("GAME_SERVER_PORT", "9200")

    assert Settings().port == 9200


@pytest.mark.parametrize("port", ["0", "65536", "not-a-port"])
def test_invalid_port_is_rejected(monkeypatch: pytest.MonkeyPatch, port: str) -> None:
    monkeypatch.setenv("GAME_SERVER_PORT", port)

    with pytest.raises(ValidationError):
        Settings()


def test_invalid_log_level_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAME_SERVER_LOG_LEVEL", "verbose")

    with pytest.raises(ValidationError):
        Settings()


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()

    assert get_settings() is get_settings()


def test_image_settings_defaults() -> None:
    settings = Settings()

    assert settings.image_base_path == Path("data/images")
    assert settings.max_image_bytes == 10 * 1024 * 1024


def test_image_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAME_SERVER_IMAGE_BASE_PATH", "/srv/images")
    monkeypatch.setenv("GAME_SERVER_MAX_IMAGE_BYTES", "2048")

    settings = Settings()

    assert settings.image_base_path == Path("/srv/images")
    assert settings.max_image_bytes == 2048


def test_non_positive_max_image_bytes_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAME_SERVER_MAX_IMAGE_BYTES", "0")

    with pytest.raises(ValidationError):
        Settings()


def test_sessions_file_defaults_to_none() -> None:
    assert Settings().sessions_file is None


def test_sessions_file_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAME_SERVER_SESSIONS_FILE", "/etc/game/sessions.json")

    assert Settings().sessions_file == Path("/etc/game/sessions.json")


def test_db_path_default() -> None:
    assert Settings().db_path == Path("data/game.sqlite3")


def test_db_path_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAME_SERVER_DB_PATH", "/srv/game.db")

    assert Settings().db_path == Path("/srv/game.db")


def test_capture_limits_defaults() -> None:
    settings = Settings()

    assert settings.max_capture_age_seconds == 300
    assert settings.max_clock_skew_seconds == 30


def test_capture_limits_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAME_SERVER_MAX_CAPTURE_AGE_SECONDS", "600")
    monkeypatch.setenv("GAME_SERVER_MAX_CLOCK_SKEW_SECONDS", "5")

    settings = Settings()

    assert (settings.max_capture_age_seconds, settings.max_clock_skew_seconds) == (600, 5)


@pytest.mark.parametrize(
    "name", ["GAME_SERVER_MAX_CAPTURE_AGE_SECONDS", "GAME_SERVER_MAX_CLOCK_SKEW_SECONDS"]
)
@pytest.mark.parametrize("value", ["0", "-1"])
def test_capture_limits_must_be_positive(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings()


def test_phash_max_distance_default() -> None:
    assert Settings().phash_max_distance == 6


@pytest.mark.parametrize("value", ["0", "32"])
def test_phash_max_distance_range_is_inclusive(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("GAME_SERVER_PHASH_MAX_DISTANCE", value)

    assert Settings().phash_max_distance == int(value)


@pytest.mark.parametrize("value", ["-1", "33"])
def test_phash_max_distance_out_of_range_is_rejected(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("GAME_SERVER_PHASH_MAX_DISTANCE", value)

    with pytest.raises(ValidationError):
        Settings()


def test_proximity_hint_interval_default() -> None:
    assert Settings().proximity_hint_interval_seconds == 10


@pytest.mark.parametrize("value", ["0", "-5"])
def test_proximity_hint_interval_must_be_positive(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("GAME_SERVER_PROXIMITY_HINT_INTERVAL_SECONDS", value)

    with pytest.raises(ValidationError):
        Settings()


def test_referee_defaults() -> None:
    settings = Settings()

    assert settings.anthropic_api_key is None
    assert settings.referee_model == "claude-haiku-4-5"
    assert settings.referee_timeout_seconds == 20
    assert settings.referee_max_retries == 2
    assert settings.referee_max_image_edge == 1568


def test_api_key_is_read_but_never_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAME_SERVER_ANTHROPIC_API_KEY", "sk-ant-SECRET-VALUE")

    settings = Settings()

    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "sk-ant-SECRET-VALUE"
    assert "SECRET-VALUE" not in repr(settings)
    assert "SECRET-VALUE" not in str(settings.model_dump())


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GAME_SERVER_REFEREE_TIMEOUT_SECONDS", "0"),
        ("GAME_SERVER_REFEREE_MAX_RETRIES", "-1"),
        ("GAME_SERVER_REFEREE_MAX_IMAGE_EDGE", "0"),
    ],
)
def test_referee_limits_are_validated(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings()


def test_zero_retries_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAME_SERVER_REFEREE_MAX_RETRIES", "0")

    assert Settings().referee_max_retries == 0

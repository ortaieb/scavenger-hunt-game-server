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
        "PORT",
        "GAME_SERVER_LOG_LEVEL",
        "GAME_SERVER_IMAGE_BASE_PATH",
        "GAME_SERVER_MAX_IMAGE_BYTES",
        "GAME_SERVER_SESSIONS_FILE",
        "GAME_SERVER_DB_URL",
        "GAME_SERVER_DB_HOST",
        "GAME_SERVER_DB_PORT",
        "GAME_SERVER_DB_NAME",
        "GAME_SERVER_DB_USER",
        "GAME_SERVER_DB_PASSWORD",
        "GAME_SERVER_DB_SSLMODE",
        "GAME_SERVER_DB_SSLROOTCERT",
        "GAME_SERVER_DB_SSLCERT",
        "GAME_SERVER_DB_SSLKEY",
        "GAME_SERVER_DB_CONNECT_TIMEOUT_SECONDS",
        "GAME_SERVER_DB_POOL_MIN_SIZE",
        "GAME_SERVER_DB_POOL_MAX_SIZE",
        "GAME_SERVER_DB_POOL_TIMEOUT_SECONDS",
        "GAME_SERVER_MAX_CAPTURE_AGE_SECONDS",
        "GAME_SERVER_MAX_CLOCK_SKEW_SECONDS",
        "GAME_SERVER_PHASH_MAX_DISTANCE",
        "GAME_SERVER_PROXIMITY_HINT_INTERVAL_SECONDS",
        "GAME_SERVER_ANTHROPIC_API_KEY",
        "GAME_SERVER_REFEREE_MODEL",
        "GAME_SERVER_REFEREE_DEADLINE_SECONDS",
        "GAME_SERVER_REFEREE_TIMEOUT_SECONDS",
        "GAME_SERVER_REFEREE_MAX_RETRIES",
        "GAME_SERVER_REFEREE_MAX_IMAGE_EDGE",
        "GAME_SERVER_REFEREE_MIN_CONFIDENCE",
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


def test_db_defaults_require_tls() -> None:
    settings = Settings()

    assert settings.db_url is None
    assert (settings.db_host, settings.db_port, settings.db_name, settings.db_user) == (
        None,
        None,
        None,
        None,
    )
    assert settings.db_password is None
    assert settings.db_sslmode == "require"
    assert (settings.db_sslrootcert, settings.db_sslcert, settings.db_sslkey) == (None, None, None)
    assert settings.db_connect_timeout_seconds == 10
    assert (settings.db_pool_min_size, settings.db_pool_max_size) == (1, 10)
    assert settings.db_pool_timeout_seconds == 10


def test_db_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAME_SERVER_DB_URL", "postgresql://game:s3cret@db.example:6543/hunt")
    monkeypatch.setenv("GAME_SERVER_DB_HOST", "db.internal")
    monkeypatch.setenv("GAME_SERVER_DB_PORT", "5433")
    monkeypatch.setenv("GAME_SERVER_DB_NAME", "game")
    monkeypatch.setenv("GAME_SERVER_DB_USER", "server")
    monkeypatch.setenv("GAME_SERVER_DB_PASSWORD", "pa55")
    monkeypatch.setenv("GAME_SERVER_DB_SSLMODE", "verify-full")
    monkeypatch.setenv("GAME_SERVER_DB_SSLROOTCERT", "system")
    monkeypatch.setenv("GAME_SERVER_DB_SSLCERT", "/certs/client.crt")
    monkeypatch.setenv("GAME_SERVER_DB_SSLKEY", "/certs/client.key")
    monkeypatch.setenv("GAME_SERVER_DB_POOL_MIN_SIZE", "2")
    monkeypatch.setenv("GAME_SERVER_DB_POOL_MAX_SIZE", "4")

    settings = Settings()

    assert settings.db_url is not None
    assert settings.db_url.get_secret_value() == "postgresql://game:s3cret@db.example:6543/hunt"
    assert (settings.db_host, settings.db_port, settings.db_name, settings.db_user) == (
        "db.internal",
        5433,
        "game",
        "server",
    )
    assert settings.db_password is not None
    assert settings.db_password.get_secret_value() == "pa55"
    assert settings.db_sslmode == "verify-full"
    assert settings.db_sslrootcert == "system"
    assert (settings.db_sslcert, settings.db_sslkey) == (
        Path("/certs/client.crt"),
        Path("/certs/client.key"),
    )
    assert (settings.db_pool_min_size, settings.db_pool_max_size) == (2, 4)


def test_db_credentials_are_not_in_the_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAME_SERVER_DB_URL", "postgresql://game:url-secret@db.example/hunt")
    monkeypatch.setenv("GAME_SERVER_DB_PASSWORD", "field-secret")

    text = repr(Settings())

    assert "url-secret" not in text
    assert "field-secret" not in text


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GAME_SERVER_DB_SSLMODE", "on"),
        ("GAME_SERVER_DB_PORT", "0"),
        ("GAME_SERVER_DB_PORT", "65536"),
        ("GAME_SERVER_DB_CONNECT_TIMEOUT_SECONDS", "0"),
        ("GAME_SERVER_DB_POOL_MIN_SIZE", "-1"),
        ("GAME_SERVER_DB_POOL_MAX_SIZE", "0"),
        ("GAME_SERVER_DB_POOL_TIMEOUT_SECONDS", "0"),
    ],
)
def test_invalid_db_settings_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings()


def test_pool_max_size_below_min_size_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAME_SERVER_DB_POOL_MIN_SIZE", "5")
    monkeypatch.setenv("GAME_SERVER_DB_POOL_MAX_SIZE", "4")

    with pytest.raises(ValidationError, match="db_pool_max_size must be at least"):
        Settings()


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
    assert settings.referee_deadline_seconds == 8
    assert settings.referee_timeout_seconds == 8
    assert settings.referee_max_retries == 2
    assert settings.referee_max_image_edge == 1568
    assert settings.referee_max_references == 2
    assert settings.referee_reference_max_edge == 768


@pytest.mark.parametrize("value", ["0", "5"])
def test_referee_max_references_accepts_zero_to_five(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("GAME_SERVER_REFEREE_MAX_REFERENCES", value)

    assert Settings().referee_max_references == int(value)


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
        ("GAME_SERVER_REFEREE_DEADLINE_SECONDS", "0"),
        ("GAME_SERVER_REFEREE_DEADLINE_SECONDS", "-8"),
        ("GAME_SERVER_REFEREE_DEADLINE_SECONDS", "soon"),
        ("GAME_SERVER_REFEREE_TIMEOUT_SECONDS", "0"),
        ("GAME_SERVER_REFEREE_MAX_RETRIES", "-1"),
        ("GAME_SERVER_REFEREE_MAX_IMAGE_EDGE", "0"),
        ("GAME_SERVER_REFEREE_MAX_REFERENCES", "-1"),
        ("GAME_SERVER_REFEREE_MAX_REFERENCES", "6"),
        ("GAME_SERVER_REFEREE_REFERENCE_MAX_EDGE", "0"),
    ],
)
def test_referee_limits_are_validated(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("value", ["0.5", "8", "30"])
def test_referee_deadline_accepts_any_positive_number(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("GAME_SERVER_REFEREE_DEADLINE_SECONDS", value)

    assert Settings().referee_deadline_seconds == float(value)


def test_the_timeout_may_exceed_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each attempt waits the smaller of the two, so neither bounds the other.
    monkeypatch.setenv("GAME_SERVER_REFEREE_DEADLINE_SECONDS", "5")
    monkeypatch.setenv("GAME_SERVER_REFEREE_TIMEOUT_SECONDS", "20")

    settings = Settings()

    assert (settings.referee_deadline_seconds, settings.referee_timeout_seconds) == (5, 20)


def test_zero_retries_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAME_SERVER_REFEREE_MAX_RETRIES", "0")

    assert Settings().referee_max_retries == 0


def test_referee_min_confidence_default() -> None:
    assert Settings().referee_min_confidence == 0.8


@pytest.mark.parametrize("value", ["0", "1", "0.65"])
def test_referee_min_confidence_accepts_zero_to_one(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("GAME_SERVER_REFEREE_MIN_CONFIDENCE", value)

    assert Settings().referee_min_confidence == float(value)


@pytest.mark.parametrize("value", ["-0.1", "1.01"])
def test_referee_min_confidence_out_of_range_is_rejected(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("GAME_SERVER_REFEREE_MIN_CONFIDENCE", value)

    with pytest.raises(ValidationError):
        Settings()


def test_tests_never_see_the_developers_env_file() -> None:
    """Guard for the shared fixture: no test can pick up a real key from a local .env."""
    assert not Path(".env").exists()
    assert Settings().anthropic_api_key is None


def test_platform_port_is_used_when_game_server_port_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PORT", "7342")  # Railway injects PORT

    assert Settings().port == 7342


def test_game_server_port_wins_over_platform_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "7342")
    monkeypatch.setenv("GAME_SERVER_PORT", "9001")

    assert Settings().port == 9001


def test_platform_port_from_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("PORT=7400\n")

    assert Settings().port == 7400


@pytest.mark.parametrize("value", ["0", "70000", "http"])
def test_invalid_platform_port_is_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PORT", value)

    with pytest.raises(ValidationError):
        Settings()


def test_port_can_still_be_set_by_name() -> None:
    assert Settings(port=9300).port == 9300

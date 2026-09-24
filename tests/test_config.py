from pathlib import Path

import pytest
from pydantic import ValidationError

from game_server.config import Settings, get_settings


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run each test in an empty directory with no GAME_SERVER_* variables set."""
    monkeypatch.chdir(tmp_path)
    for name in ("GAME_SERVER_HOST", "GAME_SERVER_PORT", "GAME_SERVER_LOG_LEVEL"):
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

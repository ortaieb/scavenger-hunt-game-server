"""Application settings, loaded from environment variables and an optional `.env` file."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["critical", "error", "warning", "info", "debug", "trace"]


class Settings(BaseSettings):
    """Runtime configuration.

    Each field can be overridden by an environment variable prefixed with `GAME_SERVER_`
    (e.g. `GAME_SERVER_PORT=9000`) or by the same key in a `.env` file. Real environment
    variables take precedence over `.env`.
    """

    model_config = SettingsConfigDict(
        env_prefix="GAME_SERVER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"  # noqa: S104 - binding all interfaces is intended inside a container
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: LogLevel = "info"
    image_base_path: Path = Path("data/images")
    max_image_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    sessions_file: Path | None = None
    db_path: Path = Path("data/game.sqlite3")
    max_capture_age_seconds: int = Field(default=300, gt=0)
    max_clock_skew_seconds: int = Field(default=30, gt=0)
    phash_max_distance: int = Field(default=6, ge=0, le=32)
    proximity_hint_interval_seconds: int = Field(default=10, gt=0)
    # Unset: the referee is disabled and never calls the API.
    anthropic_api_key: SecretStr | None = None
    referee_model: str = "claude-haiku-4-5"
    referee_timeout_seconds: float = Field(default=20, gt=0)
    referee_max_retries: int = Field(default=2, ge=0)
    referee_max_image_edge: int = Field(default=1568, gt=0)
    # The model's confidence is self-reported, not calibrated: tuned with an eval (#23).
    referee_min_confidence: float = Field(default=0.8, ge=0, le=1)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, read once on first use."""
    return Settings()

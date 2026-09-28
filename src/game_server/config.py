"""Application settings, loaded from environment variables and an optional `.env` file."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
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


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, read once on first use."""
    return Settings()

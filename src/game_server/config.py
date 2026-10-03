"""Application settings, loaded from environment variables and an optional `.env` file."""

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["critical", "error", "warning", "info", "debug", "trace"]
# libpq's sslmode: https://www.postgresql.org/docs/current/libpq-ssl.html#LIBPQ-SSL-PROTECTION
SslMode = Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]
DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024


class Settings(BaseSettings):
    """Runtime configuration.

    Each field can be overridden by an environment variable prefixed with `GAME_SERVER_`
    (e.g. `GAME_SERVER_PORT=9000`) or by the same key in a `.env` file. Real environment
    variables take precedence over `.env`.

    The port also falls back to the platform-standard `PORT` (Railway sets it for the
    port it routes traffic to): `GAME_SERVER_PORT`, then `PORT`, then 8000.
    """

    model_config = SettingsConfigDict(
        env_prefix="GAME_SERVER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        validate_by_name=True,  # Settings(port=...) still works despite the port's aliases
    )

    host: str = "0.0.0.0"  # noqa: S104 - binding all interfaces is intended inside a container
    port: int = Field(
        default=8000, ge=1, le=65535, validation_alias=AliasChoices("GAME_SERVER_PORT", "PORT")
    )
    log_level: LogLevel = "info"
    image_base_path: Path = Path("data/images")
    max_image_bytes: int = Field(default=DEFAULT_MAX_IMAGE_BYTES, gt=0)
    sessions_file: Path | None = None
    # External PostgreSQL: a URL (e.g. Railway's DATABASE_URL), the separate fields, or both.
    # A field that is set overrides the same part of the URL; libpq's PG* variables fill in
    # anything neither sets. The URL and password hold credentials: SecretStr keeps them out
    # of reprs and logs.
    db_url: SecretStr | None = None
    db_host: str | None = None
    db_port: int | None = Field(default=None, ge=1, le=65535)
    db_name: str | None = None
    db_user: str | None = None
    db_password: SecretStr | None = None
    # TLS is required by default. verify-ca/verify-full also check the server's certificate
    # against db_sslrootcert (a CA file, or "system" for the OS's trusted CAs).
    db_sslmode: SslMode = "require"
    db_sslrootcert: str | None = None
    # A client certificate and its key, for servers that authenticate clients by certificate.
    db_sslcert: Path | None = None
    db_sslkey: Path | None = None
    db_connect_timeout_seconds: int = Field(default=10, gt=0)
    # Connections kept open, at most open, and how long a request waits for a free one.
    db_pool_min_size: int = Field(default=1, ge=0)
    db_pool_max_size: int = Field(default=10, gt=0)
    db_pool_timeout_seconds: float = Field(default=10, gt=0)
    max_capture_age_seconds: int = Field(default=300, gt=0)
    max_clock_skew_seconds: int = Field(default=30, gt=0)
    phash_max_distance: int = Field(default=6, ge=0, le=32)
    proximity_hint_interval_seconds: int = Field(default=10, gt=0)
    # How long an arrival's one-time code stays valid.
    arrival_code_ttl_seconds: int = Field(default=600, gt=0)
    # Unset: the referee is disabled and never calls the API.
    anthropic_api_key: SecretStr | None = None
    referee_model: str = "claude-haiku-4-5"
    referee_timeout_seconds: float = Field(default=20, gt=0)
    referee_max_retries: int = Field(default=2, ge=0)
    referee_max_image_edge: int = Field(default=1568, gt=0)
    # The model's confidence is self-reported, not calibrated: tuned with an eval (#23).
    referee_min_confidence: float = Field(default=0.8, ge=0, le=1)

    @model_validator(mode="after")
    def _pool_sizes_are_consistent(self) -> Self:
        if self.db_pool_max_size < self.db_pool_min_size:
            raise ValueError("db_pool_max_size must be at least db_pool_min_size")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, read once on first use."""
    return Settings()

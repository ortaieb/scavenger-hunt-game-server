"""Application settings, loaded from environment variables and an optional `.env` file."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LogLevel = Literal["critical", "error", "warning", "info", "debug", "trace"]
# libpq's sslmode: https://www.postgresql.org/docs/current/libpq-ssl.html#LIBPQ-SSL-PROTECTION
SslMode = Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]
DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024
# The referee's time limits, shared with `referee.CallLimits`. docs/api.md (Referee) has the
# latency figures behind them.
DEFAULT_REFEREE_DEADLINE_SECONDS = 8.0
DEFAULT_REFEREE_TIMEOUT_SECONDS = 8.0
DEFAULT_REFEREE_MAX_RETRIES = 2
DEFAULT_DESIGNER_MIN_SPACING_M = 150.0
DesignerRunnerName = Literal["stub", "agent"]
AuthMode = Literal["off", "optional", "required"]


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
        # A validation error never echoes a value: some settings are secrets (keys, passwords).
        hide_input_in_errors=True,
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
    # The users directory's own login (`directory_app`), a URL: it takes the TLS, timeout and
    # pool settings below, never the separate db_* fields, which belong to the game's login.
    # Unset: the directory is unavailable.
    directory_db_url: SecretStr | None = None
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
    # The whole referee step, retries included, ends by the deadline; each attempt waits at
    # most the timeout, or the time left if that's less.
    referee_deadline_seconds: float = Field(default=DEFAULT_REFEREE_DEADLINE_SECONDS, gt=0)
    referee_timeout_seconds: float = Field(default=DEFAULT_REFEREE_TIMEOUT_SECONDS, gt=0)
    referee_max_retries: int = Field(default=DEFAULT_REFEREE_MAX_RETRIES, ge=0)
    referee_max_image_edge: int = Field(default=1568, gt=0)
    # A checkpoint's reference photos sent with each photo, in the sessions file's order
    # (0: none), and their long edge: they only need to show the place.
    referee_max_references: int = Field(default=2, ge=0, le=5)
    referee_reference_max_edge: int = Field(default=768, gt=0)
    # The model's confidence is self-reported, not calibrated: tuned with an eval (#23).
    referee_min_confidence: float = Field(default=0.8, ge=0, le=1)
    # `agent` runs the hunt-designer agent; `stub` fills drafts with a fixed hunt after a delay.
    designer_runner: DesignerRunnerName = "agent"
    designer_stub_delay_seconds: float = Field(default=1, ge=0)
    # The largest area a walking hunt covers, in km either way: bigger areas are clipped.
    designer_max_area_km: float = Field(default=3, gt=0)
    # The least distance between two of a draft's checkpoints, in metres.
    designer_min_spacing_m: float = Field(default=DEFAULT_DESIGNER_MIN_SPACING_M, gt=0)
    # The hunt-designer agent's limits and model: a run stops at whichever limit comes first.
    designer_max_turns: int = Field(default=30, gt=0)
    designer_max_budget_usd: float = Field(default=1.0, gt=0)
    # The SDK has no session timeout of its own: past this, the run is cut off.
    designer_deadline_seconds: float = Field(default=300, gt=0)
    designer_model: str = "claude-sonnet-5-5"
    # OpenStreetMap: Nominatim finds the area, Overpass the places in it. Public instances by
    # default; their usage policies apply (see docs/api.md, Map data).
    osm_nominatim_url: str = "https://nominatim.openstreetmap.org"
    osm_overpass_url: str = "https://overpass-api.de/api/interpreter"
    osm_timeout_seconds: float = Field(default=30, gt=0)
    # The organiser's MCP endpoint, `/mcp`, behind the organiser key. Off: not mounted.
    mcp_enabled: bool = False
    # `Host` values `/mcp` accepts (DNS-rebinding protection), comma-separated; `name:*` takes
    # any port. Empty: every request is refused.
    mcp_allowed_hosts: Annotated[list[str], NoDecode] = []
    # The web app's base URL, for links to its screens. Unset: no links.
    web_app_url: str | None = None
    # Clerk session tokens (see `auth`). `off`: not checked; `optional`: checked when sent;
    # `required`: always. Anything but `off` needs the key, the issuer and the web app's origins.
    auth_mode: AuthMode = "off"
    # Clerk's PEM public key, so a check makes no network call.
    clerk_jwt_key: str | None = None
    # Clerk's Frontend API URL, which every token's `iss` must equal.
    clerk_issuer: str | None = None
    # Origins a token's `azp` may name, comma-separated: the web app's.
    clerk_authorized_parties: Annotated[list[str], NoDecode] = []
    # Clerk's Backend API key, for writing each person's pid and role into their Clerk user.
    # Unset: nothing is written to Clerk, and /me/provision answers 503.
    clerk_secret_key: SecretStr | None = None
    # The Svix signing secret of Clerk's webhook. Unset: /internal/clerk-events answers 503.
    clerk_webhook_secret: SecretStr | None = None
    # One /me/provision per Clerk user per this many seconds.
    provision_interval_seconds: int = Field(default=5, gt=0)
    # How often the outbox's due writes to Clerk are sent, and the UTC hour of the nightly
    # reconcile with Clerk.
    clerk_sync_interval_seconds: float = Field(default=10, gt=0)
    clerk_reconcile_hour_utc: int = Field(default=3, ge=0, le=23)
    # The key of the keyed hash (HMAC-SHA256) of an email kept on the suppression list when an
    # erased person asked not to be contacted, so the address itself is never kept. Unset: such
    # an erasure waits, unfinished, until it's set. Never logged.
    suppression_key: SecretStr | None = None
    # How often unfinished erasures are retried in the background.
    erasure_sweep_interval_seconds: float = Field(default=60, gt=0)

    @field_validator("mcp_allowed_hosts", "clerk_authorized_parties", mode="before")
    @classmethod
    def _split_commas(cls, values: object) -> object:
        if isinstance(values, str):
            return [value.strip() for value in values.split(",") if value.strip()]
        return values

    @field_validator("clerk_jwt_key")
    @classmethod
    def _unescape_newlines(cls, key: str | None) -> str | None:
        # A PEM pasted into a one-line environment variable carries `\n` for its line breaks.
        return key.replace("\\n", "\n") if key else key

    @model_validator(mode="after")
    def _auth_is_configured(self) -> Self:
        if self.auth_mode != "off":
            missing = [
                name
                for name in ("clerk_jwt_key", "clerk_issuer", "clerk_authorized_parties")
                if not getattr(self, name)
            ]
            if missing:
                raise ValueError(f"auth_mode {self.auth_mode} needs {', '.join(missing)}")
        return self

    @model_validator(mode="after")
    def _pool_sizes_are_consistent(self) -> Self:
        if self.db_pool_max_size < self.db_pool_min_size:
            raise ValueError("db_pool_max_size must be at least db_pool_min_size")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, read once on first use."""
    return Settings()

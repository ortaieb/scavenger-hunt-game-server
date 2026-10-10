"""railway.toml: the health check every deploy applies to the Railway service."""

import tomllib
from pathlib import Path

from game_server.app import create_app

RAILWAY_TOML = Path(__file__).parent.parent / "railway.toml"


def deploy_settings() -> dict[str, object]:
    with RAILWAY_TOML.open("rb") as file:
        deploy: dict[str, object] = tomllib.load(file)["deploy"]
    return deploy


def test_the_health_check_path_is_the_app_s_readiness_route() -> None:
    path = deploy_settings()["healthcheckPath"]
    # Every API route, as the secrecy test finds them: included routers' routes are in the schema.
    paths = create_app().openapi()["paths"]

    assert path == "/health"
    assert "get" in paths[path]


def test_the_health_check_timeout_is_a_positive_number_of_seconds() -> None:
    timeout = deploy_settings()["healthcheckTimeout"]

    assert isinstance(timeout, int)
    assert not isinstance(timeout, bool)
    assert timeout > 0

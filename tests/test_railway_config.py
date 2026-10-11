"""Railway as code: the health check every deploy applies, and no public database endpoint."""

import re
import tomllib
from pathlib import Path

from check_migrations import git

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


def test_nothing_reaches_postgresql_through_a_public_endpoint() -> None:
    # PostgreSQL is private (#105): no workflow, config or doc may use its public TCP proxy.
    # The plan's history and this test are the only places that may name it.
    repo = RAILWAY_TOML.parent
    tracked = git("-C", str(repo), "ls-files").splitlines()
    allowed = {"docs/plans/flyway-migrations.md", "tests/test_railway_config.py"}
    public = re.compile(r"DATABASE_PUBLIC_URL|proxy\.rlwy\.net")

    offenders = [
        name
        for name in tracked
        if name not in allowed
        and (repo / name).is_file()
        and public.search((repo / name).read_text(errors="ignore"))
    ]

    assert offenders == []

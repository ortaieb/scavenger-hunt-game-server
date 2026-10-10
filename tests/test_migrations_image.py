"""The migrations image's definition: Flyway's one pin, db/ only, no root, no credentials."""

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
DOCKERFILE = (ROOT / "Dockerfile.migrations").read_text()
MAKEFILE = (ROOT / "Makefile").read_text()


def instructions() -> list[str]:
    """The Dockerfile's instructions, without comments or the check directive."""
    lines = (line.strip() for line in DOCKERFILE.splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def test_the_flyway_image_comes_from_the_makefile_pin() -> None:
    assert instructions()[:2] == ["ARG FLYWAY_IMAGE", "FROM ${FLYWAY_IMAGE}"]
    target = MAKEFILE.split("migrations-image:", 1)[1].split("\n\n", 1)[0]
    assert "-f Dockerfile.migrations --build-arg FLYWAY_IMAGE=$(FLYWAY_IMAGE)" in target


def test_only_the_settings_and_the_migrations_are_copied_in() -> None:
    copies = [line for line in instructions() if line.startswith(("COPY", "ADD"))]

    assert copies == [
        "COPY db/flyway.toml /flyway/project/flyway.toml",
        "COPY db/migrations/ /flyway/project/migrations/",
    ]


def test_the_build_context_is_only_db() -> None:
    ignore = (ROOT / "Dockerfile.migrations.dockerignore").read_text().splitlines()
    rules = [line for line in ignore if line and not line.startswith("#")]

    assert rules == ["*", "!db/flyway.toml", "!db/migrations/"]


def test_it_runs_as_a_non_root_user() -> None:
    [user] = [line for line in instructions() if line.startswith("USER ")]

    uid = user.removeprefix("USER ").split(":")[0]
    assert uid.isdigit() and int(uid) >= 1000


def test_no_credentials_are_baked_in() -> None:
    assert not re.search(r"^(ENV|ARG)\s+FLYWAY_(URL|USER|PASSWORD)", DOCKERFILE, re.MULTILINE)


def test_it_migrates_with_the_shared_settings_by_default() -> None:
    assert 'ENTRYPOINT ["flyway", "-configFiles=/flyway/project/flyway.toml"]' in instructions()
    assert instructions()[-1] == 'CMD ["migrate"]'


def test_the_app_image_still_leaves_db_out() -> None:
    rules = (ROOT / ".dockerignore").read_text().splitlines()

    assert "*" in rules
    assert not [rule for rule in rules if rule.startswith("!db")]

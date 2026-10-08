"""The designer's runners: the stub's fixed hunt, and the agent (not available yet)."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from game_server.config import Settings
from game_server.database import Database
from game_server.designer_runners import (
    AgentRunner,
    StubRunner,
    get_designer_runner,
    stub_checkpoints,
)
from game_server.drafts import DraftRequest, DraftStore
from game_server.sessions import Checkpoint, VisualChallenge

T0 = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
REQUEST = DraftRequest.model_validate({"area": "Chiswick", "theme": "Brewing"})


@pytest.fixture
def store(database: Database) -> DraftStore:
    return DraftStore(database)


def test_the_stub_hunt_passes_the_sessions_file_limits() -> None:
    for checkpoint in stub_checkpoints():
        Checkpoint(
            sequence=checkpoint.position,
            name=checkpoint.place.name,
            clue=checkpoint.clue,
            location=checkpoint.place.location,
            proximity=checkpoint.proximity,
            challenge=VisualChallenge(
                scene=checkpoint.challenge.scene, pose=checkpoint.challenge.pose
            ),
        )


def test_the_stub_hunt_has_three_distinct_places() -> None:
    checkpoints = stub_checkpoints()

    assert [c.position for c in checkpoints] == [1, 2, 3]
    assert len({c.place.location for c in checkpoints}) == 3
    assert len({c.place.osm for c in checkpoints}) == 3


def test_the_stub_finishes_after_its_delay(store: DraftStore) -> None:
    draft = uuid4()
    store.create(draft, REQUEST, "stub", T0)
    runner = StubRunner(store, lambda: T0, delay_seconds=0)

    runner.start(draft, REQUEST)
    runner.join()

    found = store.get(draft)
    assert found is not None
    assert (found.status, len(found.checkpoints), len(found.progress)) == ("ready", 3, 4)


def test_a_stopped_stub_writes_nothing(store: DraftStore) -> None:
    draft = uuid4()
    store.create(draft, REQUEST, "stub", T0)
    runner = StubRunner(store, lambda: T0, delay_seconds=60)

    runner.start(draft, REQUEST)
    runner.stop()

    found = store.get(draft)
    assert found is not None
    assert (found.status, found.progress) == ("running", ())


def test_the_agent_is_not_available_yet() -> None:
    assert AgentRunner().available() is False


@pytest.mark.parametrize(
    ("runner", "name"), [("stub", "stub"), ("agent", "agent")], ids=["stub", "agent"]
)
def test_the_setting_picks_the_runner(store: DraftStore, runner: str, name: str) -> None:
    settings = Settings(designer_runner=runner)

    assert get_designer_runner(settings, store, lambda: T0).name == name


def test_the_agent_is_the_default() -> None:
    assert Settings().designer_runner == "agent"
    assert Settings().designer_stub_delay_seconds == 1

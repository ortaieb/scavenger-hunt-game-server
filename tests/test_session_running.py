from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from game_server.checks.base import CheckResult, Rejection, SubmissionContext
from game_server.checks.session_running import (
    SESSION_NOT_STARTED,
    SESSION_STOPPED,
    SessionRunningCheck,
)
from game_server.models import ChallengeMetadata, Location
from game_server.session_runs import SessionRun
from game_server.sessions import Checkpoint, GameSession

PLANNED_START = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
PLANNED_END = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
CHECK = SessionRunningCheck()


def make_ctx(run: SessionRun | None, received_at: datetime) -> SubmissionContext:
    checkpoint = Checkpoint(
        sequence=1, name="Spot", clue="Find it", location=Location(lat=0, long=0), proximity=10
    )
    session = GameSession(
        id=UUID(int=1),
        name="Hunt",
        location="Here",
        start_time=PLANNED_START,
        end_time=PLANNED_END,
        checkpoints=(checkpoint,),
    )
    metadata = ChallengeMetadata(
        session=session.id,
        participant=UUID(int=2),
        checkpoint=1,
        location=Location(lat=0, long=0),
        capture_time=received_at,
    )
    return SubmissionContext(metadata, received_at, session, checkpoint, b"img", 0, run=run)


@pytest.mark.parametrize(
    "received_at",
    [PLANNED_START - timedelta(hours=1), PLANNED_END + timedelta(hours=1)],
    ids=["before-planned-start", "after-planned-end"],
)
def test_a_running_session_passes_whatever_the_planned_times(received_at: datetime) -> None:
    run = SessionRun(started_at=PLANNED_START - timedelta(hours=2), stopped_at=None)

    assert CHECK(make_ctx(run, received_at)) == CheckResult.passed(
        "session_running", "The session is running."
    )


@pytest.mark.parametrize(
    "run", [None, SessionRun(started_at=None, stopped_at=None)], ids=["no-run", "not-started"]
)
def test_a_session_not_yet_started_fails(run: SessionRun | None) -> None:
    result = CHECK(make_ctx(run, PLANNED_START + timedelta(minutes=5)))

    assert result == CheckResult.failed("session_running", SESSION_NOT_STARTED)


def test_a_stopped_session_fails_even_inside_the_planned_times() -> None:
    run = SessionRun(started_at=PLANNED_START, stopped_at=PLANNED_START + timedelta(minutes=30))

    result = CHECK(make_ctx(run, PLANNED_START + timedelta(hours=1)))

    assert result == CheckResult.failed("session_running", SESSION_STOPPED)


@pytest.mark.parametrize("rejection", [SESSION_NOT_STARTED, SESSION_STOPPED])
def test_messages_reveal_no_times(rejection: Rejection) -> None:
    assert not any(char.isdigit() for char in rejection.message)

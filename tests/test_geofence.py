from datetime import UTC, datetime
from uuid import UUID

import pytest
from pytest_mock import MockerFixture

from game_server.checks.base import SubmissionContext
from game_server.checks.geofence import OUT_OF_RANGE, GeofenceCheck
from game_server.models import ChallengeMetadata, Location
from game_server.sessions import Checkpoint, GameSession

CHECKPOINT_AT = Location(lat=51.5, long=-0.1)
PROXIMITY = 40


def make_ctx(submitted: Location) -> SubmissionContext:
    checkpoint = Checkpoint(
        sequence=1, name="Spot", clue="Find it", location=CHECKPOINT_AT, proximity=PROXIMITY
    )
    session = GameSession(
        id=UUID(int=1),
        name="Hunt",
        location="Here",
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        end_time=datetime(2026, 1, 2, tzinfo=UTC),
        checkpoints=(checkpoint,),
    )
    metadata = ChallengeMetadata(
        session=session.id,
        participant=UUID(int=2),
        checkpoint=1,
        location=submitted,
        capture_time=datetime(2026, 1, 1, 12, tzinfo=UTC),
    )
    return SubmissionContext(
        metadata, datetime(2026, 1, 1, 12, tzinfo=UTC), session, checkpoint, b"img"
    )


@pytest.mark.parametrize(
    ("submitted", "in_range"),
    [
        pytest.param(CHECKPOINT_AT, True, id="exactly-at-checkpoint"),
        pytest.param(Location(lat=51.5001, long=-0.1), True, id="well-inside-11m"),
        pytest.param(Location(lat=51.50035, long=-0.1), True, id="inside-39m"),
        pytest.param(Location(lat=51.5004, long=-0.1), False, id="just-outside-44m"),
        pytest.param(Location(lat=48.8584, long=2.2945), False, id="far-away-paris"),
    ],
)
def test_submitted_location(submitted: Location, in_range: bool) -> None:
    rejection = GeofenceCheck()(make_ctx(submitted))

    assert rejection == (None if in_range else OUT_OF_RANGE)


@pytest.mark.parametrize(
    ("distance", "in_range"),
    [
        pytest.param(PROXIMITY - 0.001, True, id="just-inside"),
        pytest.param(float(PROXIMITY), True, id="exactly-on-boundary"),
        pytest.param(PROXIMITY + 0.001, False, id="just-outside"),
    ],
)
def test_boundary_counts_as_in_range(
    mocker: MockerFixture, distance: float, in_range: bool
) -> None:
    mocker.patch("game_server.checks.base.geo.distance_m", return_value=distance)

    rejection = GeofenceCheck()(make_ctx(CHECKPOINT_AT))

    assert rejection == (None if in_range else OUT_OF_RANGE)


def test_message_reveals_no_distance_direction_or_coordinates() -> None:
    message = OUT_OF_RANGE.message.lower()

    assert not any(char.isdigit() for char in message)
    for word in ("north", "south", "east", "west", "metre", "meter", "km", "closer", "far"):
        assert word not in message

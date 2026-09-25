import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from game_server.models import ChallengeMetadata


@pytest.fixture
def payload() -> dict[str, Any]:
    return {
        "session": "aeffe667-4f9f-4108-b5e2-56ae821fe413",
        "participant": "7c860ccc-9adf-4e22-b54f-3ff158f5d600",
        "location": {"lat": 51.509948, "long": -1.485923},
        "capture-time": "2012-03-29T10:05:45-06:00",
    }


def test_parses_issue_example(payload: dict[str, Any]) -> None:
    metadata = ChallengeMetadata.model_validate_json(json.dumps(payload))

    assert metadata.session == UUID("aeffe667-4f9f-4108-b5e2-56ae821fe413")
    assert metadata.participant == UUID("7c860ccc-9adf-4e22-b54f-3ff158f5d600")
    assert metadata.location.lat == 51.509948
    assert metadata.location.long == -1.485923
    assert metadata.capture_time == datetime(
        2012, 3, 29, 10, 5, 45, tzinfo=timezone(timedelta(hours=-6))
    )


def test_accepts_utc_z_suffix(payload: dict[str, Any]) -> None:
    payload["capture-time"] = "2012-03-29T16:05:45Z"

    metadata = ChallengeMetadata.model_validate(payload)

    assert metadata.capture_time == datetime(2012, 3, 29, 16, 5, 45, tzinfo=UTC)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session", "not-a-uuid"),
        ("participant", 42),
        ("capture-time", "2012-03-29T10:05:45"),  # no timezone offset
        ("capture-time", "yesterday"),
        ("location", {"lat": 91, "long": 0}),
        ("location", {"lat": 0, "long": -180.5}),
        ("location", {"lat": 0}),
    ],
)
def test_rejects_invalid_field(payload: dict[str, Any], field: str, value: object) -> None:
    payload[field] = value

    with pytest.raises(ValidationError):
        ChallengeMetadata.model_validate(payload)


@pytest.mark.parametrize("field", ["session", "participant", "location", "capture-time"])
def test_rejects_missing_field(payload: dict[str, Any], field: str) -> None:
    del payload[field]

    with pytest.raises(ValidationError):
        ChallengeMetadata.model_validate(payload)


def test_rejects_unknown_field(payload: dict[str, Any]) -> None:
    payload["score"] = 100

    with pytest.raises(ValidationError):
        ChallengeMetadata.model_validate(payload)

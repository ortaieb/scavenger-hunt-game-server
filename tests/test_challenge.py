import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from pytest_mock import MockerFixture

from game_server.app import create_app
from game_server.checks import Check, Rejection, SubmissionContext, get_checks
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.sessions import SessionRepository, get_session_repository, parse_sessions

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00fake-jpeg-body\xff\xd9"
MAX_IMAGE_BYTES = 1024

SESSION = "aeffe667-4f9f-4108-b5e2-56ae821fe413"
OTHER_SESSION = "0b5e9c1e-2f7a-4d8e-9a57-3c1f6f0d2b44"
PARTICIPANT = "7c860ccc-9adf-4e22-b54f-3ff158f5d600"
OTHER_PARTICIPANT = "5d0a8b8e-7f6c-4d4b-8f0e-2b1a9c3d4e5f"
NOW = datetime(2026, 10, 3, 9, 30, tzinfo=UTC)

METADATA: dict[str, Any] = {
    "session": SESSION,
    "participant": PARTICIPANT,
    "checkpoint": 1,
    "location": {"lat": 51.509948, "long": -1.485923},
    "capture-time": "2026-10-03T10:29:00+01:00",
}


def sessions_repository() -> SessionRepository:
    checkpoint = {"name": "Spot", "clue": "Find it", "location": {"lat": 51.5, "long": -0.1}}
    return parse_sessions(
        json.dumps(
            [
                {
                    "id": SESSION,
                    "name": "Test hunt",
                    "location": "Somewhere",
                    "start-time": "2026-10-03T10:00:00+01:00",
                    "end-time": "2026-10-03T13:00:00+01:00",
                    "checkpoints": [
                        {**checkpoint, "sequence": 1, "proximity": 40},
                        {**checkpoint, "sequence": 2, "proximity": 25},
                    ],
                }
            ]
        )
    )


def rejecting(code: str) -> Check:
    return lambda ctx: Rejection(code, f"Rejected: {code}")


def accepting(ctx: SubmissionContext) -> None:
    return None


@pytest.fixture
def image_dir(tmp_path: Path) -> Path:
    return tmp_path / "images"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "game.sqlite3"


@pytest.fixture
def checks() -> list[Check]:
    """Checks the endpoint runs; tests append fakes before posting."""
    return []


@pytest.fixture
def client(image_dir: Path, db_path: Path, checks: list[Check]) -> Iterator[TestClient]:
    app = create_app()
    settings = Settings(image_base_path=image_dir, max_image_bytes=MAX_IMAGE_BYTES, db_path=db_path)
    repository = sessions_repository()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: repository
    app.dependency_overrides[get_checks] = lambda: checks
    app.dependency_overrides[get_clock] = lambda: lambda: NOW
    with TestClient(app) as test_client:
        yield test_client


def post_challenge(
    client: TestClient,
    metadata: str | dict[str, Any] = METADATA,
    image: bytes = JPEG,
    image_type: str = "image/jpeg",
) -> Response:
    raw = metadata if isinstance(metadata, str) else json.dumps(metadata)
    return client.post(
        "/challenge",
        files={
            "metadata": (None, raw, "application/json"),
            "challenge-image": ("photo.jpeg", image, image_type),
        },
    )


def stored_files(image_dir: Path) -> list[Path]:
    return sorted(image_dir.glob("*")) if image_dir.exists() else []


def stored_rows(db_path: Path) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM submissions ORDER BY id").fetchall()


def checkpoint_verdict(response: Response) -> dict[str, Any]:
    result: dict[str, Any] = response.json()["verdict"]["checkpoint"]
    return result


# --- verdicts ----------------------------------------------------------------


def test_no_rejections_gives_pending_202(client: TestClient) -> None:
    response = post_challenge(client)

    assert response.status_code == 202
    body = response.json()
    assert body == {
        "verdict": {
            "game": SESSION,
            "participant": PARTICIPANT,
            "checkpoint": {
                "sequence": 1,
                "attempt": 1,
                "time": "2026-10-03T09:30:00Z",
                "verdict": "pending",
                "rejections": [],
            },
        },
        "image_id": body["image_id"],
    }
    UUID(body["image_id"])


def test_accepting_check_still_gives_pending(client: TestClient, checks: list[Check]) -> None:
    checks.append(accepting)

    response = post_challenge(client)

    assert response.status_code == 202
    assert checkpoint_verdict(response)["verdict"] == "pending"


def test_rejecting_check_gives_failed_200(client: TestClient, checks: list[Check]) -> None:
    checks.extend([accepting, rejecting("outside_window")])

    response = post_challenge(client)

    assert response.status_code == 200
    assert checkpoint_verdict(response)["verdict"] == "failed"
    assert checkpoint_verdict(response)["rejections"] == [
        {"code": "outside_window", "message": "Rejected: outside_window"}
    ]


def test_all_rejections_are_listed(client: TestClient, checks: list[Check]) -> None:
    checks.extend([rejecting("outside_window"), accepting, rejecting("outside_geofence")])

    response = post_challenge(client)

    assert [r["code"] for r in checkpoint_verdict(response)["rejections"]] == [
        "outside_window",
        "outside_geofence",
    ]


def test_checks_see_server_data(client: TestClient, checks: list[Check]) -> None:
    seen: list[SubmissionContext] = []
    checks.append(lambda ctx: seen.append(ctx))

    post_challenge(client, {**METADATA, "checkpoint": 2})

    [ctx] = seen
    assert ctx.received_at == NOW
    assert ctx.session.id == UUID(SESSION)
    assert ctx.checkpoint.sequence == 2
    assert ctx.checkpoint.proximity == 25
    assert ctx.image == JPEG
    assert ctx.metadata.participant == UUID(PARTICIPANT)


# --- received-at -------------------------------------------------------------


@pytest.mark.parametrize(
    "capture_time", ["1999-01-01T00:00:00Z", "2099-12-31T23:59:59-11:00", "2026-10-03T09:30:00Z"]
)
def test_time_is_received_at_not_capture_time(client: TestClient, capture_time: str) -> None:
    response = post_challenge(client, {**METADATA, "capture-time": capture_time})

    assert checkpoint_verdict(response)["time"] == "2026-10-03T09:30:00Z"


def test_received_at_is_normalised_to_utc(image_dir: Path, db_path: Path) -> None:
    app = create_app()
    settings = Settings(image_base_path=image_dir, db_path=db_path)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = sessions_repository
    app.dependency_overrides[get_clock] = lambda: (
        lambda: NOW.astimezone(timezone(timedelta(hours=5)))
    )
    with TestClient(app) as test_client:
        response = post_challenge(test_client)

    assert checkpoint_verdict(response)["time"] == "2026-10-03T09:30:00Z"


# --- attempts ----------------------------------------------------------------


def test_attempts_count_up_per_participant_and_checkpoint(client: TestClient) -> None:
    other_checkpoint = {**METADATA, "checkpoint": 2}
    other_participant = {**METADATA, "participant": OTHER_PARTICIPANT}
    submissions = [METADATA, METADATA, other_checkpoint, METADATA, other_participant]

    attempts = [
        checkpoint_verdict(post_challenge(client, metadata))["attempt"] for metadata in submissions
    ]

    assert attempts == [1, 2, 1, 3, 1]


def test_failed_submissions_count_as_attempts(client: TestClient, checks: list[Check]) -> None:
    checks.append(rejecting("outside_window"))
    post_challenge(client)
    checks.clear()

    assert checkpoint_verdict(post_challenge(client))["attempt"] == 2


# --- storage -----------------------------------------------------------------


def test_records_submission_row(client: TestClient, checks: list[Check], db_path: Path) -> None:
    checks.append(rejecting("outside_window"))

    image_id = post_challenge(client).json()["image_id"]

    [row] = stored_rows(db_path)
    assert dict(row) == {
        "id": row["id"],
        "session": SESSION,
        "participant": PARTICIPANT,
        "checkpoint": 1,
        "attempt": 1,
        "received_at": "2026-10-03T09:30:00+00:00",
        "capture_time": "2026-10-03T10:29:00+01:00",
        "lat": 51.509948,
        "long": -1.485923,
        "image_id": image_id,
        "verdict": "failed",
        "rejections": json.dumps(
            [{"code": "outside_window", "message": "Rejected: outside_window"}]
        ),
    }


def test_stores_image_named_by_image_id(client: TestClient, image_dir: Path) -> None:
    image_id = post_challenge(client).json()["image_id"]

    stored = image_dir.resolve() / f"{image_id}.jpeg"
    assert stored_files(image_dir) == [stored]
    assert stored.read_bytes() == JPEG


def test_image_removed_if_recording_fails(
    client: TestClient, image_dir: Path, mocker: MockerFixture
) -> None:
    mocker.patch(
        "game_server.submissions.SubmissionStore.record", side_effect=sqlite3.OperationalError
    )

    with pytest.raises(sqlite3.OperationalError):
        post_challenge(client)

    assert stored_files(image_dir) == []


def test_logs_received_challenge(
    client: TestClient, checks: list[Check], image_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    checks.extend([rejecting("outside_window"), rejecting("outside_geofence")])
    caplog.set_level(logging.INFO, logger="game_server")

    image_id = post_challenge(client).json()["image_id"]

    assert caplog.messages == [
        f"Received challenge request for {SESSION}[{PARTICIPANT}] "
        "arrived at 2026-10-03T10:29:00+01:00 from (51.509948,-1.485923), "
        f"image stored in: {image_dir.resolve() / f'{image_id}.jpeg'}; "
        "checkpoint 1 attempt 1 verdict failed rejections [outside_window,outside_geofence]"
    ]


# --- rejected requests store nothing ----------------------------------------


def assert_nothing_stored(image_dir: Path, db_path: Path) -> None:
    assert stored_files(image_dir) == []
    assert stored_rows(db_path) == []


@pytest.mark.parametrize(
    ("metadata", "detail"),
    [
        ({**METADATA, "session": OTHER_SESSION}, "unknown session"),
        ({**METADATA, "checkpoint": 3}, "unknown checkpoint"),
    ],
)
def test_unknown_target_is_404(
    client: TestClient, image_dir: Path, db_path: Path, metadata: dict[str, Any], detail: str
) -> None:
    response = post_challenge(client, metadata)

    assert response.status_code == 404
    assert response.json() == {"detail": detail}
    assert_nothing_stored(image_dir, db_path)


@pytest.mark.parametrize(
    "metadata",
    [
        "not json",
        {**METADATA, "session": "not-a-uuid"},
        {k: v for k, v in METADATA.items() if k != "capture-time"},
        {k: v for k, v in METADATA.items() if k != "checkpoint"},
        {**METADATA, "checkpoint": 0},
        {**METADATA, "checkpoint": -1},
        {**METADATA, "checkpoint": "1"},
        {**METADATA, "checkpoint": 1.5},
        {**METADATA, "checkpoint": 1.0},
        {**METADATA, "checkpoint": True},
        {**METADATA, "verified": True},
        {**METADATA, "in-range": True},
        {**METADATA, "location": {**METADATA["location"], "within-proximity": True}},
    ],
)
def test_invalid_metadata_is_422(
    client: TestClient, image_dir: Path, db_path: Path, metadata: str | dict[str, Any]
) -> None:
    response = post_challenge(client, metadata)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][:2] == ["body", "metadata"]
    assert_nothing_stored(image_dir, db_path)


def test_rejects_non_jpeg_content_type(client: TestClient, image_dir: Path, db_path: Path) -> None:
    response = post_challenge(client, image_type="image/png")

    assert response.status_code == 415
    assert_nothing_stored(image_dir, db_path)


@pytest.mark.parametrize("image", [b"", b"\x89PNG\r\n\x1a\n-not-a-jpeg"])
def test_rejects_body_that_is_not_jpeg(
    client: TestClient, image_dir: Path, db_path: Path, image: bytes
) -> None:
    response = post_challenge(client, image=image)

    assert response.status_code == 422
    assert_nothing_stored(image_dir, db_path)


def test_accepts_image_at_size_limit(client: TestClient) -> None:
    image = JPEG + b"\x00" * (MAX_IMAGE_BYTES - len(JPEG))

    assert post_challenge(client, image=image).status_code == 202


def test_rejects_image_over_size_limit(client: TestClient, image_dir: Path, db_path: Path) -> None:
    image = JPEG + b"\x00" * (MAX_IMAGE_BYTES - len(JPEG) + 1)

    response = post_challenge(client, image=image)

    assert response.status_code == 413
    assert_nothing_stored(image_dir, db_path)


@pytest.mark.parametrize("missing", ["metadata", "challenge-image"])
def test_rejects_missing_part(client: TestClient, missing: str) -> None:
    files: dict[str, tuple[str | None, bytes | str, str]] = {
        "metadata": (None, json.dumps(METADATA), "application/json"),
        "challenge-image": ("photo.jpeg", JPEG, "image/jpeg"),
    }
    del files[missing]

    response = client.post("/challenge", files=files)

    assert response.status_code == 422

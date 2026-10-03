import json
import logging
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from images import jpeg, scene
from psycopg.rows import DictRow
from pytest_mock import MockerFixture

from game_server.app import create_app
from game_server.challenge import judge_and_record
from game_server.checks import Check, CheckResult, Rejection, SubmissionContext, get_checks
from game_server.checks.duplicate_photo import DuplicatePhotoCheck
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.models import ChallengeMetadata
from game_server.phash import perceptual_hash, to_hex
from game_server.sessions import SessionRepository, get_session_repository, parse_sessions
from game_server.storage import ImageStore
from game_server.submissions import SubmissionStore

JPEG = jpeg(scene(0, (16, 16)), quality=50)  # a real, decodable JPEG under 1 KiB
MAX_IMAGE_BYTES = 1024

SESSION = "aeffe667-4f9f-4108-b5e2-56ae821fe413"
OTHER_SESSION = "0b5e9c1e-2f7a-4d8e-9a57-3c1f6f0d2b44"  # not loaded: unknown
SECOND_SESSION = "9d2f4c1a-6b3e-4f8a-9c7d-1e2f3a4b5c6d"
PARTICIPANT = "7c860ccc-9adf-4e22-b54f-3ff158f5d600"
OTHER_PARTICIPANT = "5d0a8b8e-7f6c-4d4b-8f0e-2b1a9c3d4e5f"
NOW = datetime(2026, 10, 3, 9, 30, tzinfo=UTC)

METADATA: dict[str, Any] = {
    "session": SESSION,
    "participant": PARTICIPANT,
    "checkpoint": 1,
    "location": {"lat": 51.5001, "long": -0.1},  # ~11 m from both test checkpoints
    "capture-time": "2026-10-03T10:29:00+01:00",
}


def sessions_repository() -> SessionRepository:
    """Two identical sessions: SESSION and SECOND_SESSION."""
    checkpoint = {"name": "Spot", "clue": "Find it", "location": {"lat": 51.5, "long": -0.1}}
    session = {
        "name": "Test hunt",
        "location": "Somewhere",
        "start-time": "2026-10-03T10:00:00+01:00",
        "end-time": "2026-10-03T13:00:00+01:00",
        "checkpoints": [
            {**checkpoint, "sequence": 1, "proximity": 40},
            {**checkpoint, "sequence": 2, "proximity": 25},
        ],
    }
    return parse_sessions(
        json.dumps([{**session, "id": SESSION}, {**session, "id": SECOND_SESSION}])
    )


def rejecting(code: str) -> Check:
    return lambda ctx: CheckResult.failed(f"no_{code}", Rejection(code, f"Rejected: {code}"))


def accepting(ctx: SubmissionContext) -> CheckResult:
    return CheckResult.passed("accepting", "Fine.")


@pytest.fixture
def image_dir(tmp_path: Path) -> Path:
    return tmp_path / "images"


@pytest.fixture
def checks() -> list[Check]:
    """Checks the endpoint runs; tests append fakes before posting."""
    return []


@pytest.fixture
def client(image_dir: Path, checks: list[Check]) -> Iterator[TestClient]:
    app = create_app()
    settings = Settings(image_base_path=image_dir, max_image_bytes=MAX_IMAGE_BYTES)
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


def stored_rows(db: psycopg.Connection[DictRow]) -> list[DictRow]:
    return db.execute("SELECT * FROM submissions ORDER BY id").fetchall()


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
                "checks": [],
                "rejections": [],
            },
        },
        "image_id": body["image_id"],
    }
    UUID(body["image_id"])


def test_every_check_passing_gives_pass_200(client: TestClient, checks: list[Check]) -> None:
    checks.append(accepting)

    response = post_challenge(client)

    assert response.status_code == 200
    assert checkpoint_verdict(response)["verdict"] == "pass"


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

    def spy(ctx: SubmissionContext) -> CheckResult:
        seen.append(ctx)
        return accepting(ctx)

    checks.append(spy)

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


def test_received_at_is_normalised_to_utc(image_dir: Path, db: psycopg.Connection[DictRow]) -> None:
    app = create_app()
    settings = Settings(image_base_path=image_dir)
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


def test_records_submission_row(
    client: TestClient, checks: list[Check], db: psycopg.Connection[DictRow]
) -> None:
    checks.append(rejecting("outside_window"))

    image_id = post_challenge(client).json()["image_id"]

    [row] = stored_rows(db)
    assert dict(row) == {
        "id": row["id"],
        "session": UUID(SESSION),
        "participant": UUID(PARTICIPANT),
        "checkpoint": 1,
        "attempt": 1,
        "received_at": NOW,
        "capture_time": datetime(2026, 10, 3, 9, 29, tzinfo=UTC),  # the instant sent
        "lat": 51.5001,
        "long": -0.1,
        "image_id": UUID(image_id),
        "verdict": "failed",
        "rejections": [{"code": "outside_window", "message": "Rejected: outside_window"}],
        "distance_m": pytest.approx(11.1, abs=0.1),
        "phash": to_hex(perceptual_hash(JPEG)),
        "phash_match_id": None,
        "checks": [
            {
                "check": "no_outside_window",
                "outcome": "failed",
                "confidence": 1.0,
                "reason": "Rejected: outside_window",
                "detail": None,
            }
        ],
        # The referee isn't consulted: the checkpoint has no challenge and a check failed.
        "referee_status": None,
        "referee_model": None,
        "referee_error": None,
        "referee_judgement": None,
        "referee_input_tokens": None,
        "referee_output_tokens": None,
        "referee_latency_ms": None,
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
        "game_server.submissions.SubmissionTransaction.record", side_effect=psycopg.OperationalError
    )

    with pytest.raises(psycopg.OperationalError):
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
        "arrived at 2026-10-03T10:29:00+01:00 from (51.5001,-0.1), "
        f"image stored in: {image_dir.resolve() / f'{image_id}.jpeg'}; "
        "checkpoint 1 attempt 1 distance 11.1m "
        "referee not_consulted "
        "verdict failed checks [no_outside_window:failed,no_outside_geofence:failed] "
        "rejections [outside_window,outside_geofence]"
    ]


# --- rejected requests store nothing ----------------------------------------


def assert_nothing_stored(image_dir: Path, db: psycopg.Connection[DictRow]) -> None:
    assert stored_files(image_dir) == []
    assert stored_rows(db) == []


@pytest.mark.parametrize(
    ("metadata", "detail"),
    [
        ({**METADATA, "session": OTHER_SESSION}, "unknown session"),
        ({**METADATA, "checkpoint": 3}, "unknown checkpoint"),
    ],
)
def test_unknown_target_is_404(
    client: TestClient,
    image_dir: Path,
    db: psycopg.Connection[DictRow],
    metadata: dict[str, Any],
    detail: str,
) -> None:
    response = post_challenge(client, metadata)

    assert response.status_code == 404
    assert response.json() == {"detail": detail}
    assert_nothing_stored(image_dir, db)


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
    client: TestClient,
    image_dir: Path,
    db: psycopg.Connection[DictRow],
    metadata: str | dict[str, Any],
) -> None:
    response = post_challenge(client, metadata)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][:2] == ["body", "metadata"]
    assert_nothing_stored(image_dir, db)


def test_rejects_non_jpeg_content_type(
    client: TestClient, image_dir: Path, db: psycopg.Connection[DictRow]
) -> None:
    response = post_challenge(client, image_type="image/png")

    assert response.status_code == 415
    assert_nothing_stored(image_dir, db)


@pytest.mark.parametrize("image", [b"", b"\x89PNG\r\n\x1a\n-not-a-jpeg"])
def test_rejects_body_that_is_not_jpeg(
    client: TestClient, image_dir: Path, db: psycopg.Connection[DictRow], image: bytes
) -> None:
    response = post_challenge(client, image=image)

    assert response.status_code == 422
    assert_nothing_stored(image_dir, db)


def test_accepts_image_at_size_limit(client: TestClient) -> None:
    image = JPEG + b"\x00" * (MAX_IMAGE_BYTES - len(JPEG))

    assert post_challenge(client, image=image).status_code == 202


def test_rejects_image_over_size_limit(
    client: TestClient, image_dir: Path, db: psycopg.Connection[DictRow]
) -> None:
    image = JPEG + b"\x00" * (MAX_IMAGE_BYTES - len(JPEG) + 1)

    response = post_challenge(client, image=image)

    assert response.status_code == 413
    assert_nothing_stored(image_dir, db)


@pytest.mark.parametrize("missing", ["metadata", "challenge-image"])
def test_rejects_missing_part(client: TestClient, missing: str) -> None:
    files: dict[str, tuple[str | None, bytes | str, str]] = {
        "metadata": (None, json.dumps(METADATA), "application/json"),
        "challenge-image": ("photo.jpeg", JPEG, "image/jpeg"),
    }
    del files[missing]

    response = client.post("/challenge", files=files)

    assert response.status_code == 422


# --- registered checks, end to end -------------------------------------------


@pytest.fixture
def clock_now() -> list[datetime]:
    """The time the real-checks client's clock returns; tests replace element 0."""
    return [NOW]


@pytest.fixture
def real_checks_client(
    image_dir: Path, db: psycopg.Connection[DictRow], clock_now: list[datetime]
) -> Iterator[TestClient]:
    """Like `client`, but with the registered checks rather than fakes."""
    app = create_app()
    settings = Settings(image_base_path=image_dir)
    repository = sessions_repository()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: repository
    app.dependency_overrides[get_clock] = lambda: lambda: clock_now[0]
    with TestClient(app) as test_client:
        yield test_client


def test_time_rules_pass_inside_window(real_checks_client: TestClient) -> None:
    response = post_challenge(real_checks_client)

    assert response.status_code == 202
    assert checkpoint_verdict(response)["verdict"] == "pending"


def test_received_after_session_end_fails(
    real_checks_client: TestClient, clock_now: list[datetime], db: psycopg.Connection[DictRow]
) -> None:
    clock_now[0] = datetime(2026, 10, 3, 12, 0, 1, tzinfo=UTC)  # session ends 12:00Z
    # Capture time inside the session and fresh: the claim can't rescue the submission.
    metadata = {**METADATA, "capture-time": "2026-10-03T13:00:00+01:00"}

    response = post_challenge(real_checks_client, metadata)

    assert response.status_code == 200
    assert checkpoint_verdict(response)["verdict"] == "failed"
    assert [r["code"] for r in checkpoint_verdict(response)["rejections"]] == ["outside_window"]
    [row] = stored_rows(db)
    assert row["verdict"] == "failed"


@pytest.mark.parametrize(
    ("capture_time", "code"),
    [
        ("2026-10-03T09:24:59Z", "stale_capture"),  # 301 s before NOW
        ("2026-10-03T03:30:31-06:00", "capture_in_future"),  # 31 s after NOW
    ],
)
def test_capture_time_claims_can_fail_submission(
    real_checks_client: TestClient, capture_time: str, code: str
) -> None:
    response = post_challenge(real_checks_client, {**METADATA, "capture-time": capture_time})

    assert response.status_code == 200
    assert [r["code"] for r in checkpoint_verdict(response)["rejections"]] == [code]


FAR_AWAY = {"lat": 51.51, "long": -0.1}  # ~1.1 km north of the checkpoints


def test_out_of_range_submission_fails(
    real_checks_client: TestClient, db: psycopg.Connection[DictRow]
) -> None:
    response = post_challenge(real_checks_client, {**METADATA, "location": FAR_AWAY})

    assert response.status_code == 200
    assert checkpoint_verdict(response)["verdict"] == "failed"
    assert [r["code"] for r in checkpoint_verdict(response)["rejections"]] == ["out_of_range"]
    [row] = stored_rows(db)
    assert row["distance_m"] == pytest.approx(1112, abs=1)


def test_in_range_submission_is_pending_never_pass(
    real_checks_client: TestClient, db: psycopg.Connection[DictRow]
) -> None:
    response = post_challenge(real_checks_client)

    assert response.status_code == 202
    assert checkpoint_verdict(response)["verdict"] == "pending"
    [row] = stored_rows(db)
    assert row["distance_m"] == pytest.approx(11.1, abs=0.1)


def json_numbers(value: object) -> list[float]:
    """Every number anywhere in a decoded JSON document."""
    if isinstance(value, bool):
        return []
    if isinstance(value, int | float):
        return [value]
    if isinstance(value, dict):
        return [n for item in value.values() for n in json_numbers(item)]
    if isinstance(value, list):
        return [n for item in value for n in json_numbers(item)]
    return []


@pytest.mark.parametrize(
    "location", [METADATA["location"], FAR_AWAY], ids=["in-range", "out-of-range"]
)
def test_response_leaks_no_checkpoint_coordinates_or_distance(
    real_checks_client: TestClient, location: dict[str, float]
) -> None:
    response = post_challenge(real_checks_client, {**METADATA, "location": location})

    body = response.json()
    checks = body["verdict"]["checkpoint"].pop("checks")
    confidences = [check["confidence"] for check in checks]
    # Deterministic checks report 1.0; the skipped visual checks (no challenge) report 0.
    assert set(confidences) <= {0.0, 1.0}
    # Apart from the checks' confidences, the only numbers are the sequence and attempt.
    assert sorted(json_numbers(body)) == [1, 1]
    assert sorted(json_numbers(checks)) == sorted(confidences)
    text = response.text.lower()
    for leak in ("51.5", "-0.1", "distance", "lat", "long", "metre", "meter"):
        assert leak not in text


# --- duplicate photos, end to end --------------------------------------------

PHOTO = jpeg(scene(7))


def codes(response: Response) -> list[str]:
    return [r["code"] for r in checkpoint_verdict(response)["rejections"]]


def test_another_participants_accepted_photo_is_a_duplicate(
    real_checks_client: TestClient, db: psycopg.Connection[DictRow]
) -> None:
    first = post_challenge(real_checks_client, image=PHOTO)
    second = post_challenge(
        real_checks_client, {**METADATA, "participant": OTHER_PARTICIPANT}, image=PHOTO
    )

    assert first.status_code == 202
    assert second.status_code == 200
    assert codes(second) == ["duplicate_photo"]
    accepted, duplicate = stored_rows(db)
    assert duplicate["phash_match_id"] == accepted["id"]
    assert accepted["phash_match_id"] is None
    assert duplicate["phash"] == accepted["phash"]


@pytest.mark.parametrize(
    "variant",
    [
        pytest.param(jpeg(scene(7), quality=45), id="re-encoded"),
        pytest.param(jpeg(scene(7).resize((320, 240))), id="resized-50%"),
        pytest.param(jpeg(scene(7).rotate(90, expand=True), orientation=6), id="exif-rotated"),
    ],
)
def test_altered_copy_of_accepted_photo_is_a_duplicate(
    real_checks_client: TestClient, variant: bytes
) -> None:
    post_challenge(real_checks_client, image=PHOTO)

    response = post_challenge(real_checks_client, {**METADATA, "checkpoint": 2}, image=variant)

    assert codes(response) == ["duplicate_photo"]


def test_different_photo_is_not_a_duplicate(real_checks_client: TestClient) -> None:
    post_challenge(real_checks_client, image=PHOTO)

    response = post_challenge(
        real_checks_client, {**METADATA, "checkpoint": 2}, image=jpeg(scene(8))
    )

    assert response.status_code == 202


def test_photo_from_a_failed_attempt_can_be_resubmitted(
    real_checks_client: TestClient, db: psycopg.Connection[DictRow]
) -> None:
    failed = post_challenge(real_checks_client, {**METADATA, "location": FAR_AWAY}, image=PHOTO)
    retry = post_challenge(real_checks_client, image=PHOTO)

    assert codes(failed) == ["out_of_range"]
    assert retry.status_code == 202
    assert [row["attempt"] for row in stored_rows(db)] == [1, 2]


def test_photos_are_never_compared_across_sessions(real_checks_client: TestClient) -> None:
    post_challenge(real_checks_client, image=PHOTO)

    response = post_challenge(
        real_checks_client, {**METADATA, "session": SECOND_SESSION}, image=PHOTO
    )

    assert response.status_code == 202


def test_duplicate_response_reveals_no_match(real_checks_client: TestClient) -> None:
    post_challenge(real_checks_client, image=PHOTO)

    response = post_challenge(
        real_checks_client, {**METADATA, "participant": OTHER_PARTICIPANT}, image=PHOTO
    )

    [rejection] = checkpoint_verdict(response)["rejections"]
    assert set(rejection) == {"code", "message"}
    assert PARTICIPANT not in response.text
    assert "matched" not in response.text


@pytest.mark.parametrize(
    "image",
    [
        pytest.param(PHOTO[:3000], id="truncated"),
        pytest.param(b"\xff\xd8\xff" + b"junk" * 100, id="jpeg-magic-then-junk"),
    ],
)
def test_undecodable_jpeg_is_422_and_stores_nothing(
    real_checks_client: TestClient, image_dir: Path, db: psycopg.Connection[DictRow], image: bytes
) -> None:
    response = post_challenge(real_checks_client, image=image)

    assert response.status_code == 422
    assert response.json() == {"detail": "challenge-image could not be decoded"}
    assert_nothing_stored(image_dir, db)


def test_concurrent_uploads_of_one_photo_accept_exactly_one(
    image_dir: Path, store: SubmissionStore
) -> None:
    """Both requests pass the checks' own logic unless the snapshot and insert are atomic."""
    barrier = Barrier(2)

    def slow_check(ctx: SubmissionContext) -> CheckResult:
        time.sleep(0.2)  # widen the window between reading accepted photos and inserting
        return accepting(ctx)

    checks: list[Check] = [DuplicatePhotoCheck(max_distance=6), slow_check]
    images = ImageStore(image_dir)
    repository = sessions_repository()
    session = repository.get_session(UUID(SESSION))
    checkpoint = repository.get_checkpoint(UUID(SESSION), 1)
    assert session is not None
    assert checkpoint is not None

    def submit(participant: str) -> str:
        metadata = ChallengeMetadata.model_validate({**METADATA, "participant": participant})
        ctx = SubmissionContext(metadata, NOW, session, checkpoint, PHOTO, perceptual_hash(PHOTO))
        barrier.wait()
        submission, _, _ = judge_and_record(ctx, checks, images, store)
        return submission.verdict

    with ThreadPoolExecutor(max_workers=2) as pool:
        verdicts = list(pool.map(submit, [PARTICIPANT, OTHER_PARTICIPANT]))

    assert sorted(verdicts) == ["failed", "pass"]


# --- every check in the verdict (#19) ----------------------------------------

REGISTRY_ORDER = [
    "window_open",
    "capture_fresh",
    "capture_time_plausible",
    "in_range",
    "photo_unique",
    "scene_matches",
    "pose_correct",
]
VISUAL = {"scene_matches", "pose_correct"}


def outcomes(response: Response) -> dict[str, str]:
    return {c["check"]: c["outcome"] for c in checkpoint_verdict(response)["checks"]}


def test_without_a_challenge_visual_checks_skip_and_verdict_is_pending(
    real_checks_client: TestClient,
) -> None:
    response = post_challenge(real_checks_client)  # the test checkpoints have no challenge

    verdict = checkpoint_verdict(response)
    assert response.status_code == 202
    assert verdict["verdict"] == "pending"
    assert verdict["rejections"] == []
    assert [c["check"] for c in verdict["checks"]] == REGISTRY_ORDER
    assert {(c["check"], c["outcome"], c["confidence"]) for c in verdict["checks"]} == {
        *((name, "passed", 1.0) for name in REGISTRY_ORDER if name not in VISUAL),
        *((name, "skipped", 0.0) for name in VISUAL),
    }
    for check in verdict["checks"]:
        assert set(check) == {"check", "outcome", "confidence", "reason"}


@pytest.mark.parametrize(
    ("changes", "now", "check", "code", "message"),
    [
        pytest.param(
            {"capture-time": "2026-10-03T12:00:00Z"},
            datetime(2026, 10, 3, 12, 0, 1, tzinfo=UTC),
            "window_open",
            "outside_window",
            "This checkpoint isn't open right now.",
            id="window",
        ),
        pytest.param(
            {"capture-time": "2026-10-03T09:24:59Z"},
            NOW,
            "capture_fresh",
            "stale_capture",
            "Photo was taken too long ago, please take a new one.",
            id="stale",
        ),
        pytest.param(
            {"capture-time": "2026-10-03T09:30:31Z"},
            NOW,
            "capture_time_plausible",
            "capture_in_future",
            "Photo's capture time is ahead of the server's clock. Check your phone's date and "
            "time, then take a new one.",
            id="future",
        ),
        pytest.param(
            {"location": FAR_AWAY},
            NOW,
            "in_range",
            "out_of_range",
            "Your location is outside the checkpoint area.",
            id="geofence",
        ),
    ],
)
def test_each_failure_keeps_its_rejection_and_shows_in_checks(
    real_checks_client: TestClient,
    clock_now: list[datetime],
    changes: dict[str, Any],
    now: datetime,
    check: str,
    code: str,
    message: str,
) -> None:
    clock_now[0] = now

    response = post_challenge(real_checks_client, {**METADATA, **changes})

    verdict = checkpoint_verdict(response)
    assert response.status_code == 200
    assert verdict["verdict"] == "failed"
    assert verdict["rejections"] == [{"code": code, "message": message}]
    assert outcomes(response) == {
        name: "failed" if name == check else "skipped" if name in VISUAL else "passed"
        for name in REGISTRY_ORDER
    }
    [failed] = [c for c in verdict["checks"] if c["outcome"] == "failed"]
    assert failed["reason"] == message


def test_duplicate_failure_shows_in_checks(real_checks_client: TestClient) -> None:
    post_challenge(real_checks_client, image=PHOTO)

    response = post_challenge(
        real_checks_client, {**METADATA, "participant": OTHER_PARTICIPANT}, image=PHOTO
    )

    assert codes(response) == ["duplicate_photo"]
    assert outcomes(response)["photo_unique"] == "failed"


def test_checks_complete_and_ordered_when_several_fail(
    real_checks_client: TestClient, clock_now: list[datetime]
) -> None:
    clock_now[0] = datetime(2026, 10, 3, 12, 30, tzinfo=UTC)  # after the session
    metadata = {**METADATA, "location": FAR_AWAY}  # capture-time 09:29Z: stale as well

    response = post_challenge(real_checks_client, metadata)

    verdict = checkpoint_verdict(response)
    assert [c["check"] for c in verdict["checks"]] == REGISTRY_ORDER
    assert [c["outcome"] for c in verdict["checks"]] == [
        "failed",
        "failed",
        "passed",
        "failed",
        "passed",
        "skipped",
        "skipped",
    ]
    assert [r["code"] for r in verdict["rejections"]] == [
        "outside_window",
        "stale_capture",
        "out_of_range",
    ]


SENTINEL = "MODERATOR-ONLY-7f3a9c"


def test_detail_is_stored_but_never_returned(
    client: TestClient, checks: list[Check], db: psycopg.Connection[DictRow]
) -> None:
    checks.append(
        lambda ctx: CheckResult(
            "scene_matches", "uncertain", 0.5, "We couldn't tell.", detail=SENTINEL
        )
    )

    response = post_challenge(client)

    assert SENTINEL not in response.text
    assert "detail" not in response.text
    assert checkpoint_verdict(response)["checks"] == [
        {
            "check": "scene_matches",
            "outcome": "uncertain",
            "confidence": 0.5,
            "reason": "We couldn't tell.",
        }
    ]
    [row] = stored_rows(db)
    assert row["checks"][0]["detail"] == SENTINEL


def test_check_schema_has_no_detail(client: TestClient) -> None:
    schemas = client.get("/openapi.json").json()["components"]["schemas"]

    assert set(schemas["CheckOut"]["properties"]) == {"check", "outcome", "confidence", "reason"}

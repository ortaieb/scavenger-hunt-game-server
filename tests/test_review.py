"""The moderator's review queue: `GET /sessions/{session}/review`."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from images import jpeg, scene

from game_server.app import create_app
from game_server.checks import CheckResult
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.models import VerdictStatus
from game_server.referee import (
    RefereeCall,
    RefereeJudgement,
    RefereeReport,
    SentImage,
    VisualCheckJudgement,
)
from game_server.review import RECENT_SHOWN
from game_server.sessions import SessionRepository, get_session_repository, parse_sessions
from game_server.submissions import NewSubmission, SubmissionStore

SESSION = "aeffe667-4f9f-4108-b5e2-56ae821fe413"
OTHER = "0b5e9c1e-2f7a-4d8e-9a57-3c1f6f0d2b44"
MODERATOR = "MOD-8H3T-QX"
OTHER_MODERATOR = "MOD-2KV9-ZP"
FOX, HERON = "FOX-7Q2K", "HERON-4MXP"
START = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
DURING = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
LAT, LONG = 51.5034, -0.1276
POSE = "Arms raised as if flying, facing the camera"
SCENE = "A stone fountain with a lion's head spout"
CHECKED_IN = CheckResult("checked_in", "passed", 1.0, "You checked in.", detail="arrival 1")
POSE_UNSURE = CheckResult(
    "pose_correct",
    "uncertain",
    0.62,
    "The referee couldn't decide on this. A moderator will review your photo.",
    detail="One arm raised, not both.",
)
CALL = RefereeCall("You are the referee.", "<scene>…</scene>", SentImage("ab" * 32, 1568, 1045))
UNSURE = VisualCheckJudgement(reason="One arm raised.", verdict="unsure", confidence=0.62)
OK = RefereeReport(
    status="ok",
    judgement=RefereeJudgement(scene_matches=UNSURE, pose_correct=UNSURE),
    model="claude-haiku-4-5",
    latency_ms=950,
    call=CALL,
)
DEADLINE = RefereeReport(
    status="error", error_code="deadline", model="claude-haiku-4-5", latency_ms=8000, call=CALL
)


def session_json(
    session_id: str, moderator: str, suffix: str = "", pose: str = POSE
) -> dict[str, Any]:
    return {
        "id": session_id,
        "name": "Hunt",
        "location": "Here",
        "start-time": START.isoformat(),
        "end-time": (START + timedelta(hours=3)).isoformat(),
        "moderator-code": moderator,
        "checkpoints": [
            {
                "sequence": 1,
                "name": "Lion fountain",
                "clue": "Clue 1",
                "location": {"lat": LAT, "long": LONG},
                "proximity": 40,
                "challenge": {"scene": SCENE, "pose": pose},
                "reference-photos": ["reference/1a.jpg", "reference/1b.jpg", "reference/1c.jpg"],
            },
            {
                "sequence": 2,
                "name": "Bandstand",
                "clue": "Clue 2",
                "location": {"lat": LAT, "long": LONG},
                "proximity": 40,
            },
        ],
        "teams": [
            {"name": "Foxes", "join-code": f"{FOX}{suffix}", "order": [1, 2]},
            {"name": "Herons", "join-code": f"{HERON}{suffix}", "order": [2, 1]},
        ],
    }


def load_sessions(tmp_path: Path, pose: str = POSE) -> SessionRepository:
    sessions = [session_json(SESSION, MODERATOR, pose=pose), session_json(OTHER, OTHER_MODERATOR)]
    sessions[1]["teams"] = []
    return parse_sessions(json.dumps(sessions), reference_dir=tmp_path)


@pytest.fixture
def repository(tmp_path: Path) -> list[SessionRepository]:
    """The sessions file the app reads: replace the element to change it mid-test."""
    (tmp_path / "reference").mkdir()
    for seed, name in enumerate(("1a", "1b", "1c")):
        (tmp_path / "reference" / f"{name}.jpg").write_bytes(jpeg(scene(seed, (64, 48))))
    return [load_sessions(tmp_path)]


@pytest.fixture
def now() -> list[datetime]:
    return [DURING]


@pytest.fixture
def client(
    tmp_path: Path, repository: list[SessionRepository], now: list[datetime]
) -> Iterator[TestClient]:
    app = create_app()
    settings = Settings(image_base_path=tmp_path / "images")
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: repository[0]
    app.dependency_overrides[get_clock] = lambda: lambda: now[0]
    with TestClient(app) as test_client:
        yield test_client


def get_review(
    client: TestClient, session: str = SESSION, authorization: str | None = f"Bearer {MODERATOR}"
) -> Response:
    headers = {"Authorization": authorization} if authorization else {}
    return client.get(f"/sessions/{session}/review", headers=headers)


def review(client: TestClient) -> dict[str, Any]:
    response = get_review(client)
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def join(client: TestClient, code: str = FOX) -> UUID:
    return UUID(client.post("/join", json={"code": code, "consent": True}).json()["participant"])


def arrive(store: SubmissionStore, participant: UUID, pose: str | None = POSE) -> int:
    """Check the team in at checkpoint 1, issuing `pose`; returns the arrival's id."""
    return store.arrive(
        UUID(SESSION),
        participant,
        1,
        pose=pose,
        now=DURING,
        ttl=timedelta(minutes=10),
        new_code=lambda: "K7Q2",
    ).arrival.id


def record(
    store: SubmissionStore,
    participant: UUID,
    *,
    verdict: VerdictStatus = "pending",
    at: datetime = DURING,
    checkpoint: int = 1,
    session: str = SESSION,
    checks: tuple[CheckResult, ...] = (),
    referee: RefereeReport | None = None,
    arrival_id: int | None = None,
) -> int:
    """Record a submission directly; returns its id."""
    return store.record(
        NewSubmission(
            session=UUID(session),
            participant=participant,
            checkpoint=checkpoint,
            received_at=at,
            capture_time=at,
            lat=LAT,
            long=LONG,
            image_id=uuid4(),
            verdict=verdict,
            checks=checks,
            distance_m=1.0,
            phash=0,
            processing_ms=40,
            referee=referee,
            arrival_id=arrival_id,
        )
    ).id


def photograph(client: TestClient, participant: UUID) -> Response:
    """Send a photo of checkpoint 1 through `POST /challenge`."""
    metadata = {
        "session": SESSION,
        "participant": str(participant),
        "checkpoint": 1,
        "location": {"lat": LAT, "long": LONG},
        "capture-time": DURING.isoformat(),
    }
    return client.post(
        "/challenge",
        files={
            "metadata": (None, json.dumps(metadata), "application/json"),
            "challenge-image": ("p.jpeg", jpeg(scene(7)), "image/jpeg"),
        },
    )


def rule(client: TestClient, submission: int, ruling: str = "approve", **body: Any) -> None:
    response = client.post(
        f"/sessions/{SESSION}/submissions/{submission}/ruling",
        json={"ruling": ruling, **body},
        headers={"Authorization": f"Bearer {MODERATOR}"},
    )
    assert response.status_code in (200, 201)


def minutes(n: int) -> datetime:
    return DURING + timedelta(minutes=n)


def queued_ids(client: TestClient) -> list[int]:
    return [photo["submission"] for photo in review(client)["to-review"]]


# --- to-review -------------------------------------------------------------------------------


def test_an_empty_session(client: TestClient, store: SubmissionStore) -> None:
    record(store, uuid4(), session=OTHER)  # another session's photo stays out

    assert review(client) == {"to-review": [], "recent": []}


def test_a_photo_to_review_beside_what_it_should_show(
    client: TestClient, store: SubmissionStore
) -> None:
    fox = join(client)
    arrival = arrive(store, fox)
    submission = record(
        store,
        fox,
        at=DURING.replace(second=5, microsecond=120),
        checks=(CHECKED_IN, POSE_UNSURE),
        referee=OK,
        arrival_id=arrival,
    )

    assert review(client)["to-review"] == [
        {
            "submission": submission,
            "team": "Foxes",
            "checkpoint": {"sequence": 1, "name": "Lion fountain"},
            "attempt": 1,
            "received-at": "2026-10-03T10:00:05Z",
            "pose": POSE,
            "scene": SCENE,
            "reference-photos": 3,
            "checks": [
                {
                    "check": "checked_in",
                    "outcome": "passed",
                    "confidence": 1.0,
                    "reason": "You checked in.",
                    "detail": "arrival 1",
                },
                {
                    "check": "pose_correct",
                    "outcome": "uncertain",
                    "confidence": 0.62,
                    "reason": POSE_UNSURE.reason,
                    "detail": "One arm raised, not both.",
                },
            ],
            "referee": {"status": "ok", "error-code": None},
        }
    ]


def test_only_unruled_pending_photos_are_listed_oldest_first(
    client: TestClient, store: SubmissionStore
) -> None:
    fox, heron = join(client), join(client, HERON)
    later = record(store, fox, at=minutes(3))
    record(store, fox, verdict="pass", at=minutes(1))
    record(store, heron, verdict="failed", at=minutes(1))
    earlier = record(store, heron, at=minutes(2))
    ruled = record(store, heron, at=minutes(0))
    rule(client, ruled, "reject")

    assert queued_ids(client) == [earlier, later]


def test_photos_received_at_once_are_in_submission_order(
    client: TestClient, store: SubmissionStore
) -> None:
    fox, heron = join(client), join(client, HERON)
    first = record(store, heron)
    second = record(store, fox)

    assert queued_ids(client) == [first, second]


def test_the_queue_matches_the_overviews_count(client: TestClient, store: SubmissionStore) -> None:
    fox = join(client)
    for at in range(3):
        record(store, fox, at=minutes(at))
    rule(client, record(store, fox, at=minutes(4)))

    overview = client.get(
        f"/sessions/{SESSION}/overview", headers={"Authorization": f"Bearer {MODERATOR}"}
    ).json()

    assert overview["to-review"] == len(review(client)["to-review"]) == 3


def test_the_pose_is_the_one_issued_at_check_in(
    client: TestClient, repository: list[SessionRepository], tmp_path: Path
) -> None:
    fox = join(client)
    client.post(f"/sessions/{SESSION}/start", headers={"Authorization": f"Bearer {MODERATOR}"})
    issued = client.post(f"/sessions/{SESSION}/participants/{fox}/arrive", json={"checkpoint": 1})
    assert issued.json()["pose"] == POSE
    assert photograph(client, fox).json()["verdict"]["checkpoint"]["verdict"] == "pending"
    repository[0] = load_sessions(tmp_path, pose="Sitting on the edge, looking up")

    [photo] = review(client)["to-review"]

    assert photo["pose"] == POSE


def test_no_pose_without_a_check_in_that_issued_one(
    client: TestClient, store: SubmissionStore
) -> None:
    fox = join(client)
    record(store, fox, arrival_id=arrive(store, fox, pose=None))
    record(store, fox, at=minutes(1))

    assert [photo["pose"] for photo in review(client)["to-review"]] == [None, None]


def test_an_errored_referee_call_says_why(client: TestClient, store: SubmissionStore) -> None:
    record(store, join(client), referee=DEADLINE)

    [photo] = review(client)["to-review"]

    assert photo["referee"] == {"status": "error", "error-code": "deadline"}


def test_no_referee_when_it_was_not_called(client: TestClient, store: SubmissionStore) -> None:
    record(store, join(client), referee=RefereeReport(status="disabled"))

    [photo] = review(client)["to-review"]

    assert photo["referee"] is None


def test_a_checkpoint_without_a_challenge_or_references(
    client: TestClient, store: SubmissionStore
) -> None:
    record(store, join(client), checkpoint=2)

    [photo] = review(client)["to-review"]

    assert photo["checkpoint"] == {"sequence": 2, "name": "Bandstand"}
    assert (photo["scene"], photo["reference-photos"]) == (None, 0)


def test_a_checkpoint_no_longer_in_the_file(client: TestClient, store: SubmissionStore) -> None:
    record(store, join(client), checkpoint=9)

    [photo] = review(client)["to-review"]

    assert photo["checkpoint"] == {"sequence": 9, "name": None}
    assert (photo["scene"], photo["reference-photos"]) == (None, 0)


def test_a_photo_without_a_participant_row_is_still_listed(
    client: TestClient, store: SubmissionStore
) -> None:
    submission = record(store, uuid4())

    [photo] = review(client)["to-review"]

    assert (photo["submission"], photo["team"]) == (submission, None)


# --- recent ----------------------------------------------------------------------------------


def test_a_ruling_moves_the_photo_from_the_queue_to_recent(
    client: TestClient, store: SubmissionStore
) -> None:
    submission = record(store, join(client))
    rule(client, submission, "approve", note="The arm is just cropped")

    assert review(client) == {
        "to-review": [],
        "recent": [
            {
                "submission": submission,
                "team": "Foxes",
                "checkpoint": {"sequence": 1, "name": "Lion fountain"},
                "ruling": "approve",
                "note": "The arm is just cropped",
                "ruled-at": "2026-10-03T10:00:00Z",
                "verdict": "pending",
            }
        ],
    }


def test_a_changed_ruling_is_shown_once_as_it_now_stands(
    client: TestClient, store: SubmissionStore
) -> None:
    submission = record(store, join(client), verdict="pass")
    rule(client, submission, "reject")
    rule(client, submission, "approve")

    [ruled] = review(client)["recent"]

    assert (ruled["submission"], ruled["ruling"], ruled["verdict"]) == (
        submission,
        "approve",
        "pass",
    )


def test_recent_is_the_latest_rulings_newest_first(
    client: TestClient, store: SubmissionStore, now: list[datetime]
) -> None:
    fox = join(client)
    submissions = [record(store, fox, at=minutes(n)) for n in range(RECENT_SHOWN + 1)]
    for n, submission in enumerate(reversed(submissions)):  # the oldest photo is ruled last
        now[0] = minutes(10 + n)
        rule(client, submission)

    recent = review(client)["recent"]

    assert [r["submission"] for r in recent] == submissions[:RECENT_SHOWN]
    assert recent[0]["ruled-at"] == "2026-10-03T10:30:00Z"


# --- errors ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer MOD-WRONG-1", f"Bearer {OTHER_MODERATOR}", f"Bearer {FOX}"],
    ids=["no-code", "wrong", "other-session", "join-code"],
)
def test_moderator_code_required(client: TestClient, authorization: str | None) -> None:
    response = get_review(client, authorization=authorization)

    assert response.status_code == 401
    assert response.json() == {
        "detail": "moderator code required",
        "code": "moderator_unauthorised",
    }
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_unknown_session_is_404(client: TestClient) -> None:
    response = get_review(client, session=str(uuid4()))

    assert (response.status_code, response.json()) == (404, {"detail": "unknown session"})

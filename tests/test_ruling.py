"""The moderator's ruling on a photo, and scoring following it:
`POST /sessions/{session}/submissions/{submission}/ruling`."""

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from images import jpeg, scene
from psycopg.rows import DictRow

from game_server.app import create_app
from game_server.checks import CheckResult
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.models import VerdictStatus
from game_server.referee import RefereeCall, RefereeReport, SentImage
from game_server.ruling import NOTE_MAX_LENGTH
from game_server.sessions import get_session_repository, parse_sessions
from game_server.submissions import NewSubmission, SubmissionStore

SESSION = "aeffe667-4f9f-4108-b5e2-56ae821fe413"
OTHER = "0b5e9c1e-2f7a-4d8e-9a57-3c1f6f0d2b44"
MODERATOR = "MOD-8H3T-QX"
OTHER_MODERATOR = "MOD-2KV9-ZP"
FOX, HERON, OWL = "FOX-7Q2K", "HERON-4MXP", "OWL-9KD3"
START = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
DURING = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
LAT, LONG = 51.5034, -0.1276
NOTE = "Pose is right, the arm is just cropped"
POSE_UNSURE = CheckResult(
    "pose_correct", "uncertain", 0.6, "A moderator will review it.", detail="Arms half up."
)
TIMEOUT = RefereeReport(
    status="error",
    error_code="timeout",
    model="claude-haiku-4-5",
    latency_ms=8000,
    call=RefereeCall(
        "You are the referee.", "<scene>A fountain</scene>", SentImage("ab" * 32, 1568, 1045)
    ),
)


def session_json(session_id: str, moderator: str, suffix: str = "") -> dict[str, Any]:
    return {
        "id": session_id,
        "name": "Hunt",
        "location": "Here",
        "start-time": START.isoformat(),
        "end-time": (START + timedelta(hours=3)).isoformat(),
        "moderator-code": moderator,
        "checkpoints": [
            {
                "sequence": n,
                "name": f"Spot {n}",
                "clue": f"Clue {n}",
                "location": {"lat": LAT, "long": LONG},
                "proximity": 40,
                "challenge": {"scene": f"Scene {n}", "pose": "Wave"},
            }
            for n in (1, 2, 3)
        ],
        "teams": [
            {"name": "Red Foxes", "join-code": f"{FOX}{suffix}", "order": [1, 2, 3]},
            {"name": "Blue Herons", "join-code": f"{HERON}{suffix}", "order": [3, 1, 2]},
            {"name": "Green Owls", "join-code": f"{OWL}{suffix}", "order": [2, 3, 1]},
        ],
    }


@pytest.fixture
def now() -> list[datetime]:
    return [DURING]


@pytest.fixture
def client(now: list[datetime], tmp_path: Path) -> Iterator[TestClient]:
    app = create_app()
    settings = Settings(image_base_path=tmp_path / "images")
    sessions = parse_sessions(
        json.dumps(
            [session_json(SESSION, MODERATOR), session_json(OTHER, OTHER_MODERATOR, "-OTHER")]
        )
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: sessions
    app.dependency_overrides[get_clock] = lambda: lambda: now[0]
    with TestClient(app) as test_client:
        yield test_client


def moderate(client: TestClient, action: str) -> Response:
    headers = {"Authorization": f"Bearer {MODERATOR}"}
    if action == "overview":
        return client.get(f"/sessions/{SESSION}/overview", headers=headers)
    return client.post(f"/sessions/{SESSION}/{action}", headers=headers)


def rule(
    client: TestClient,
    submission: int,
    ruling: str = "approve",
    note: str | None = None,
    *,
    session: str = SESSION,
    code: str = MODERATOR,
) -> Response:
    body: dict[str, Any] = {"ruling": ruling} if note is None else {"ruling": ruling, "note": note}
    return client.post(
        f"/sessions/{session}/submissions/{submission}/ruling",
        json=body,
        headers={"Authorization": f"Bearer {code}"},
    )


def join(client: TestClient, code: str = FOX) -> str:
    participant: str = client.post("/join", json={"code": code, "consent": True}).json()[
        "participant"
    ]
    return participant


def submit(
    store: SubmissionStore,
    participant: str,
    checkpoint: int,
    verdict: VerdictStatus,
    at: datetime = DURING,
    *,
    session: str = SESSION,
    checks: tuple[CheckResult, ...] = (),
    referee: RefereeReport | None = None,
) -> int:
    return store.record(
        NewSubmission(
            session=UUID(session),
            participant=UUID(participant),
            checkpoint=checkpoint,
            received_at=at,
            capture_time=at,
            lat=LAT,
            long=LONG,
            image_id=uuid4(),
            verdict=verdict,
            checks=checks,
            distance_m=1.0,
            phash=checkpoint,
            processing_ms=40,
            referee=referee,
        )
    ).id


def state(client: TestClient, participant: str) -> dict[str, Any]:
    response = client.get(f"/sessions/{SESSION}/participants/{participant}/state")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def overview(client: TestClient) -> dict[str, Any]:
    response = moderate(client, "overview")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def team_row(client: TestClient, name: str) -> dict[str, Any]:
    row: dict[str, Any] = next(t for t in overview(client)["teams"] if t["team"] == name)
    return row


def minutes(n: int) -> datetime:
    return DURING + timedelta(minutes=n)


# --- the response ----------------------------------------------------------------------------


def test_a_first_ruling_is_201_with_both_verdicts(
    client: TestClient, store: SubmissionStore, now: list[datetime]
) -> None:
    fox = join(client)
    moderate(client, "start")
    submission = submit(store, fox, 1, "pending")
    now[0] = minutes(5)

    response = rule(client, submission, "approve", NOTE)

    assert response.status_code == 201
    assert response.json() == {
        "submission": submission,
        "verdict": "pending",
        "ruling": {"ruling": "approve", "note": NOTE, "ruled-at": "2026-10-03T10:05:00Z"},
        "effective-verdict": "pass",
    }


def test_a_ruling_without_a_note(client: TestClient, store: SubmissionStore) -> None:
    submission = submit(store, join(client), 1, "pass")

    body = rule(client, submission, "reject").json()

    assert body["ruling"]["note"] is None
    assert (body["verdict"], body["effective-verdict"]) == ("pass", "failed")


def test_a_second_ruling_replaces_the_first_and_both_are_kept(
    client: TestClient, store: SubmissionStore, db: psycopg.Connection[DictRow], now: list[datetime]
) -> None:
    submission = submit(store, join(client), 1, "pending")
    assert rule(client, submission, "approve", "Looks fine").status_code == 201
    now[0] = minutes(9)

    response = rule(client, submission, "reject", "On second look, wrong fountain")

    assert response.status_code == 200
    assert response.json()["ruling"] == {
        "ruling": "reject",
        "note": "On second look, wrong fountain",
        "ruled-at": "2026-10-03T10:09:00Z",
    }
    assert response.json()["effective-verdict"] == "failed"
    rows = db.execute(
        "SELECT ruling, note FROM rulings WHERE submission_id = %s ORDER BY id", (submission,)
    )
    assert [(r["ruling"], r["note"]) for r in rows.fetchall()] == [
        ("approve", "Looks fine"),
        ("reject", "On second look, wrong fountain"),
    ]


def test_the_referees_verdict_checks_and_trace_are_unchanged(
    client: TestClient, store: SubmissionStore, db: psycopg.Connection[DictRow]
) -> None:
    submission = submit(store, join(client), 1, "pending", checks=(POSE_UNSURE,), referee=TIMEOUT)
    before = (
        db.execute("SELECT * FROM submissions").fetchall(),
        db.execute("SELECT * FROM referee_traces").fetchall(),
    )
    assert before[1]  # not vacuous: the submission has a trace

    rule(client, submission, "approve")
    rule(client, submission, "reject")

    after = (
        db.execute("SELECT * FROM submissions").fetchall(),
        db.execute("SELECT * FROM referee_traces").fetchall(),
    )
    assert after == before


@pytest.mark.parametrize("phase", ["scheduled", "running", "stopped"])
def test_a_ruling_is_allowed_in_any_phase(
    client: TestClient, store: SubmissionStore, phase: str
) -> None:
    submission = submit(store, join(client), 1, "pending")
    if phase != "scheduled":
        moderate(client, "start")
    if phase == "stopped":
        moderate(client, "stop")

    assert rule(client, submission).status_code == 201


def test_each_ruling_is_logged_without_the_note(
    client: TestClient, store: SubmissionStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    submission = submit(store, join(client), 1, "failed")

    rule(client, submission, "approve", "SENTINEL-NOTE-a3 the arm is cropped")
    rule(client, submission, "reject", "SENTINEL-NOTE-a3 changed my mind")

    lines = [r.getMessage() for r in caplog.records if r.name == "game_server.ruling"]
    assert lines == [
        f"Moderator ruling session {SESSION} submission {submission} approve"
        " verdict failed -> pass first=yes",
        f"Moderator ruling session {SESSION} submission {submission} reject"
        " verdict failed -> failed first=no",
    ]
    assert "SENTINEL-NOTE-a3" not in caplog.text


# --- errors ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer MOD-WRONG-1", f"Bearer {OTHER_MODERATOR}", f"Bearer {FOX}"],
    ids=["no-code", "wrong", "other-session", "join-code"],
)
def test_moderator_code_required(
    client: TestClient,
    store: SubmissionStore,
    db: psycopg.Connection[DictRow],
    authorization: str | None,
) -> None:
    submission = submit(store, join(client), 1, "pending")
    headers = {"Authorization": authorization} if authorization else {}

    response = client.post(
        f"/sessions/{SESSION}/submissions/{submission}/ruling",
        json={"ruling": "approve"},
        headers=headers,
    )

    assert response.status_code == 401
    assert response.json() == {
        "detail": "moderator code required",
        "code": "moderator_unauthorised",
    }
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert db.execute("SELECT * FROM rulings").fetchall() == []


def test_the_code_is_checked_before_the_body(client: TestClient) -> None:
    response = client.post(f"/sessions/{SESSION}/submissions/1/ruling", json={"ruling": "maybe"})

    assert response.status_code == 401


def test_unknown_session_is_404(client: TestClient) -> None:
    response = rule(client, 1, session=str(uuid4()))

    assert response.status_code == 404
    assert response.json() == {"detail": "unknown session"}


def test_another_sessions_submission_is_404(
    client: TestClient, store: SubmissionStore, db: psycopg.Connection[DictRow]
) -> None:
    elsewhere = submit(store, str(uuid4()), 1, "pending", session=OTHER)

    response = rule(client, elsewhere)

    assert response.status_code == 404
    assert response.json() == {"detail": "unknown submission"}
    assert rule(client, elsewhere, session=OTHER, code=OTHER_MODERATOR).status_code == 201
    assert db.execute("SELECT session FROM rulings").fetchall() == [{"session": UUID(OTHER)}]


def test_an_unknown_submission_is_404(client: TestClient, store: SubmissionStore) -> None:
    submission = submit(store, join(client), 1, "pending")

    response = rule(client, submission + 1)

    assert (response.status_code, response.json()) == (404, {"detail": "unknown submission"})


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"ruling": "maybe"},
        {"ruling": "APPROVE"},
        {"ruling": None},
        {"ruling": "approve", "note": "x" * (NOTE_MAX_LENGTH + 1)},
        {"ruling": "approve", "note": 7},
        {"ruling": "approve", "points": -2},
        [],
    ],
    ids=[
        "no-ruling",
        "unknown-ruling",
        "upper-case",
        "null-ruling",
        "note-too-long",
        "note-not-text",
        "unknown-field",
        "not-an-object",
    ],
)
def test_an_invalid_body_is_422(
    client: TestClient, store: SubmissionStore, db: psycopg.Connection[DictRow], body: Any
) -> None:
    submission = submit(store, join(client), 1, "pending")

    response = client.post(
        f"/sessions/{SESSION}/submissions/{submission}/ruling",
        json=body,
        headers={"Authorization": f"Bearer {MODERATOR}"},
    )

    assert response.status_code == 422
    assert db.execute("SELECT * FROM rulings").fetchall() == []


def test_a_note_of_the_longest_length_is_kept(client: TestClient, store: SubmissionStore) -> None:
    submission = submit(store, join(client), 1, "pending")
    note = "x" * NOTE_MAX_LENGTH

    assert rule(client, submission, "approve", note).json()["ruling"]["note"] == note


@pytest.mark.parametrize("submission", ["0", "-1", str(2**63), "abc"])
def test_a_malformed_submission_id_is_422(client: TestClient, submission: str) -> None:
    response = client.post(
        f"/sessions/{SESSION}/submissions/{submission}/ruling",
        json={"ruling": "approve"},
        headers={"Authorization": f"Bearer {MODERATOR}"},
    )

    assert response.status_code == 422


# --- scoring follows the ruling ----------------------------------------------------------------


def test_approving_a_pending_photo_places_it_by_when_it_was_received(
    client: TestClient, store: SubmissionStore, now: list[datetime]
) -> None:
    fox, heron, owl = join(client), join(client, HERON), join(client, OWL)
    moderate(client, "start")
    pending = submit(store, fox, 1, "pending", minutes(1))
    submit(store, heron, 1, "pass", minutes(2))
    submit(store, owl, 1, "pass", minutes(3))
    # Three teams joined: an unplaced checkpoint counts 4.
    assert state(client, fox)["score"] == {
        "points": 4 + 4 + 4,
        "in-review": 1,
        "final": False,
        "place": None,
    }
    assert state(client, heron)["score"]["points"] == 4 + 1 + 4
    assert state(client, owl)["score"]["points"] == 4 + 4 + 2
    assert overview(client)["to-review"] == 1
    now[0] = minutes(30)  # ruled long after the other photos arrived

    rule(client, pending, "approve")

    assert state(client, fox)["score"] == {
        "points": 1 + 4 + 4,
        "in-review": 0,
        "final": False,
        "place": None,
    }
    assert state(client, heron)["score"]["points"] == 4 + 2 + 4  # one place down
    assert state(client, owl)["score"]["points"] == 4 + 4 + 3
    body = overview(client)
    assert body["to-review"] == 0
    assert [(t["team"], t["points"], t["in-review"]) for t in body["teams"]] == [
        ("Red Foxes", 9, 0),
        ("Blue Herons", 10, 0),
        ("Green Owls", 11, 0),
    ]


def test_rejecting_a_pending_photo_keeps_progress_and_scores_n_plus_one(
    client: TestClient, store: SubmissionStore
) -> None:
    fox, _ = join(client), join(client, HERON)
    moderate(client, "start")
    pending = submit(store, fox, 1, "pending")
    before = state(client, fox)

    rule(client, pending, "reject")

    after = state(client, fox)
    assert after["progress"] == before["progress"] == {"completed": 1, "total": 3}
    assert after["current"] == before["current"]
    assert after["current"]["sequence"] == 2
    assert after["score"] == {"points": 3 + 3 + 3, "in-review": 0, "final": False, "place": None}
    assert overview(client)["to-review"] == 0


def test_approving_a_failed_photo_completes_the_checkpoint(
    client: TestClient, store: SubmissionStore
) -> None:
    fox, _ = join(client), join(client, HERON)
    moderate(client, "start")
    failed = submit(store, fox, 1, "failed")
    assert state(client, fox)["current"]["sequence"] == 1

    rule(client, failed, "approve")

    after = state(client, fox)
    assert after["progress"] == {"completed": 1, "total": 3}
    assert (after["current"]["sequence"], after["current"]["clue"]) == (2, "Clue 2")
    assert after["score"]["points"] == 1 + 3 + 3
    row = team_row(client, "Red Foxes")
    assert (row["completed"], row["current"]["sequence"]) == (1, 2)
    assert row["last-completed"]["verdict"] == "pass"


def test_rejecting_a_pass_scores_n_plus_one_and_keeps_progress(
    client: TestClient, store: SubmissionStore
) -> None:
    fox, heron = join(client), join(client, HERON)
    moderate(client, "start")
    passed = submit(store, fox, 1, "pass", minutes(1))
    submit(store, heron, 1, "pass", minutes(2))

    rule(client, passed, "reject")

    after = state(client, fox)
    assert after["progress"] == {"completed": 1, "total": 3}
    assert after["current"]["sequence"] == 2
    assert after["score"]["points"] == 3 + 3 + 3
    assert state(client, heron)["score"]["points"] == 3 + 1 + 3  # moved up to first
    row = team_row(client, "Red Foxes")
    assert row["last-completed"]["verdict"] == "failed"  # the effective verdict
    assert row["completed"] == 1


def test_changing_an_approval_to_a_rejection_never_sends_a_team_back(
    client: TestClient, store: SubmissionStore
) -> None:
    fox = join(client)
    moderate(client, "start")
    failed = submit(store, fox, 1, "failed")
    rule(client, failed, "approve")

    rule(client, failed, "reject")

    after = state(client, fox)
    assert (after["progress"]["completed"], after["current"]["sequence"]) == (1, 2)
    assert after["score"]["points"] == 2 + 2 + 2


def test_a_rejected_photo_no_longer_blocks_the_same_photo(
    client: TestClient, db: psycopg.Connection[DictRow]
) -> None:
    fox, heron = join(client), join(client, HERON)
    moderate(client, "start")
    photo = jpeg(scene(731))
    first = photograph(client, fox, 1, photo)
    assert first["verdict"]["checkpoint"]["verdict"] == "pending"
    blocked = photograph(client, heron, 3, photo)
    assert blocked["verdict"]["checkpoint"]["rejections"][0]["code"] == "duplicate_photo"
    row = db.execute("SELECT id FROM submissions WHERE image_id = %s", (UUID(first["image_id"]),))
    submission = row.fetchone()
    assert submission is not None

    rule(client, submission["id"], "reject")

    again = photograph(client, heron, 3, photo)
    assert again["verdict"]["checkpoint"]["verdict"] == "pending"


def photograph(client: TestClient, participant: str, checkpoint: int, photo: bytes) -> Any:
    client.post(
        f"/sessions/{SESSION}/participants/{participant}/arrive", json={"checkpoint": checkpoint}
    )
    metadata = {
        "session": SESSION,
        "participant": participant,
        "checkpoint": checkpoint,
        "location": {"lat": LAT, "long": LONG},
        "capture-time": DURING.isoformat(),
    }
    return client.post(
        "/challenge",
        files={
            "metadata": (None, json.dumps(metadata), "application/json"),
            "challenge-image": ("p.jpeg", photo, "image/jpeg"),
        },
    ).json()


# --- final results wait for the reviews --------------------------------------------------------


def test_after_a_stop_the_results_wait_for_the_last_review(
    client: TestClient, store: SubmissionStore
) -> None:
    fox, heron = join(client), join(client, HERON)
    moderate(client, "start")
    submit(store, heron, 1, "pass", minutes(1))
    pending = submit(store, fox, 1, "pending", minutes(2))
    moderate(client, "stop")

    assert state(client, fox)["score"] == {
        "points": 3 + 3 + 3,
        "in-review": 1,
        "final": False,
        "place": None,
    }
    body = overview(client)
    assert body["to-review"] == 1
    assert all(t["place"] is None for t in body["teams"])

    rule(client, pending, "approve")

    assert state(client, fox)["score"] == {
        "points": 2 + 3 + 3,
        "in-review": 0,
        "final": True,
        "place": 2,
    }
    assert state(client, heron)["score"] == {
        "points": 3 + 1 + 3,
        "in-review": 0,
        "final": True,
        "place": 1,
    }
    body = overview(client)
    assert body["to-review"] == 0
    assert [(t["team"], t["place"]) for t in body["teams"]] == [
        ("Blue Herons", 1),
        ("Red Foxes", 2),
        ("Green Owls", None),
    ]


def test_a_ruled_pass_or_failed_photo_does_not_hold_up_the_results(
    client: TestClient, store: SubmissionStore
) -> None:
    fox = join(client)
    moderate(client, "start")
    submit(store, fox, 1, "pass")
    submit(store, fox, 2, "failed")
    moderate(client, "stop")

    assert state(client, fox)["score"]["final"] is True
    assert overview(client)["to-review"] == 0

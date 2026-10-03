"""POST /challenge with the referee in the verdict (#22), using a fake Referee."""

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from images import jpeg, scene
from psycopg.rows import DictRow
from pytest_mock import MockerFixture

from game_server.app import create_app
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.referee import (
    RefereeJudgement,
    RefereeReport,
    VisualCheckJudgement,
    get_referee,
)
from game_server.sessions import VisualChallenge, get_session_repository, parse_sessions
from game_server.submissions import SubmissionStore

SESSION = "aeffe667-4f9f-4108-b5e2-56ae821fe413"
NOW = datetime(2026, 10, 3, 10, 30, tzinfo=UTC)
PHOTO = jpeg(scene(11))
SCENE_REASON = "SENTINEL-SCENE-REASON granite fountain ring behind the player"
POSE_REASON = "SENTINEL-POSE-REASON facing left in profile"
CHALLENGE = {"scene": "A granite fountain in open lawn", "pose": "Side profile"}

METADATA: dict[str, Any] = {
    "session": SESSION,
    "participant": "7c860ccc-9adf-4e22-b54f-3ff158f5d600",
    "checkpoint": 1,  # has a challenge; checkpoint 2 doesn't
    "location": {"lat": 51.5001, "long": -0.1},
    "capture-time": "2026-10-03T10:29:30Z",
}

Verdict = Literal["pass", "fail", "unsure"]


def report(
    scene_verdict: Verdict = "pass",
    pose_verdict: Verdict = "pass",
    confidence: float = 0.95,
) -> RefereeReport:
    return RefereeReport(
        status="ok",
        judgement=RefereeJudgement(
            scene_matches=VisualCheckJudgement(
                reason=SCENE_REASON, verdict=scene_verdict, confidence=confidence
            ),
            pose_correct=VisualCheckJudgement(
                reason=POSE_REASON, verdict=pose_verdict, confidence=confidence
            ),
        ),
        model="claude-haiku-4-5",
        request_id="req_fake",
        input_tokens=1600,
        output_tokens=140,
        latency_ms=1234,
    )


Db = psycopg.Connection[DictRow]


def write_lock_is_free(db: Db) -> bool:
    """True if no write transaction holds a session's advisory lock."""
    row = db.execute(
        "SELECT NOT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory') AS free"
    ).fetchone()
    return row is not None and row["free"] is True


@dataclass
class FakeReferee:
    report: RefereeReport
    db: Db
    calls: list[VisualChallenge] = field(default_factory=list)
    lock_free_during_call: list[bool] = field(default_factory=list)

    def judge(self, image: bytes, challenge: VisualChallenge) -> RefereeReport:
        self.calls.append(challenge)
        self.lock_free_during_call.append(write_lock_is_free(self.db))
        return self.report


@pytest.fixture
def referee(db: Db) -> FakeReferee:
    return FakeReferee(report(), db)


@pytest.fixture
def client(tmp_path: Path, referee: FakeReferee, store: SubmissionStore) -> Iterator[TestClient]:
    store.start_run(UUID(SESSION), datetime(2026, 10, 3, 10, 0, tzinfo=UTC))
    checkpoint = {"name": "Spot", "clue": "Find it", "location": {"lat": 51.5, "long": -0.1}}
    repository = parse_sessions(
        json.dumps(
            [
                {
                    "id": SESSION,
                    "name": "Hunt",
                    "location": "Here",
                    "start-time": "2026-10-03T10:00:00Z",
                    "end-time": "2026-10-03T12:00:00Z",
                    "checkpoints": [
                        {**checkpoint, "sequence": 1, "proximity": 40, "challenge": CHALLENGE},
                        {**checkpoint, "sequence": 2, "proximity": 40},
                    ],
                }
            ]
        )
    )
    settings = Settings(image_base_path=tmp_path / "images")
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: repository
    app.dependency_overrides[get_clock] = lambda: lambda: NOW
    app.dependency_overrides[get_referee] = lambda: referee
    with TestClient(app) as test_client:
        yield test_client


def submit(client: TestClient, **changes: Any) -> Response:
    return client.post(
        "/challenge",
        files={
            "metadata": (None, json.dumps({**METADATA, **changes}), "application/json"),
            "challenge-image": ("photo.jpeg", PHOTO, "image/jpeg"),
        },
    )


def checkpoint_verdict(response: Response) -> dict[str, Any]:
    result: dict[str, Any] = response.json()["verdict"]["checkpoint"]
    return result


def outcomes(response: Response) -> dict[str, str]:
    return {c["check"]: c["outcome"] for c in checkpoint_verdict(response)["checks"]}


def rows(db: Db) -> list[DictRow]:
    return db.execute("SELECT * FROM submissions ORDER BY id").fetchall()


# --- verdicts --------------------------------------------------------------------


def test_everything_passing_awards_pass(client: TestClient, referee: FakeReferee) -> None:
    response = submit(client)

    verdict = checkpoint_verdict(response)
    assert response.status_code == 200
    assert verdict["verdict"] == "pass"
    assert verdict["rejections"] == []
    assert len(verdict["checks"]) == 8
    assert set(outcomes(response).values()) == {"passed"}
    by_name = {c["check"]: c for c in verdict["checks"]}
    assert by_name["scene_matches"]["confidence"] == 0.95
    assert by_name["scene_matches"]["reason"] == "Your photo matches this checkpoint."
    assert len(referee.calls) == 1
    assert referee.calls[0] == VisualChallenge(**CHALLENGE)


@pytest.mark.parametrize(
    ("scene_verdict", "pose_verdict", "failed_check", "code"),
    [
        ("fail", "pass", "scene_matches", "scene_mismatch"),
        ("pass", "fail", "pose_correct", "pose_incorrect"),
    ],
)
def test_confident_model_fail_fails_the_submission(
    client: TestClient,
    referee: FakeReferee,
    scene_verdict: Verdict,
    pose_verdict: Verdict,
    failed_check: str,
    code: str,
) -> None:
    referee.report = report(scene_verdict, pose_verdict, confidence=0.9)

    response = submit(client)

    verdict = checkpoint_verdict(response)
    assert response.status_code == 200
    assert verdict["verdict"] == "failed"
    assert [r["code"] for r in verdict["rejections"]] == [code]
    assert outcomes(response)[failed_check] == "failed"


@pytest.mark.parametrize(
    ("scene_verdict", "confidence"),
    [("unsure", 0.99), ("pass", 0.5)],
    ids=["model-unsure", "below-threshold"],
)
def test_unsure_or_low_confidence_is_pending(
    client: TestClient, referee: FakeReferee, scene_verdict: Verdict, confidence: float
) -> None:
    referee.report = report(scene_verdict, "pass", confidence=confidence)

    response = submit(client)

    assert response.status_code == 202
    assert checkpoint_verdict(response)["verdict"] == "pending"
    assert outcomes(response)["scene_matches"] == "uncertain"


def test_referee_error_is_pending_and_recorded(
    client: TestClient, referee: FakeReferee, db: Db
) -> None:
    referee.report = RefereeReport(
        status="error", error_code="timeout", model="claude-haiku-4-5", latency_ms=20000
    )

    response = submit(client)

    assert response.status_code == 202
    assert checkpoint_verdict(response)["verdict"] == "pending"
    assert outcomes(response)["scene_matches"] == "uncertain"
    assert outcomes(response)["pose_correct"] == "uncertain"
    [row] = rows(db)
    assert (row["referee_status"], row["referee_error"]) == ("error", "timeout")
    assert row["referee_latency_ms"] == 20000
    assert row["referee_judgement"] is None


# --- when the referee is consulted ---------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"location": {"lat": 51.51, "long": -0.1}}, id="out-of-range"),
        pytest.param({"capture-time": "2026-10-03T09:00:00Z"}, id="stale-capture"),
    ],
)
def test_earlier_failure_skips_the_referee(
    client: TestClient, referee: FakeReferee, changes: dict[str, Any], db: Db
) -> None:
    response = submit(client, **changes)

    assert checkpoint_verdict(response)["verdict"] == "failed"
    assert referee.calls == []
    assert outcomes(response)["scene_matches"] == "skipped"
    assert outcomes(response)["pose_correct"] == "skipped"
    [row] = rows(db)
    assert row["referee_status"] is None


def test_a_photo_after_the_stop_is_stored_failed_without_the_referee(
    client: TestClient, referee: FakeReferee, store: SubmissionStore, db: Db
) -> None:
    store.stop_run(UUID(SESSION), datetime(2026, 10, 3, 10, 15, tzinfo=UTC))

    response = submit(client)

    verdict = checkpoint_verdict(response)
    assert response.status_code == 200
    assert verdict["verdict"] == "failed"
    assert verdict["rejections"][0] == {
        "code": "session_stopped",
        "message": "The session is over. This photo was recorded but doesn't count.",
    }
    assert outcomes(response)["session_running"] == "failed"
    assert referee.calls == []
    [row] = rows(db)
    assert row["verdict"] == "failed"
    assert row["rejections"][0]["code"] == "session_stopped"
    assert row["referee_status"] is None


def test_a_photo_before_the_start_is_stored_failed_without_the_referee(
    client: TestClient, referee: FakeReferee, db: Db
) -> None:
    db.execute("TRUNCATE session_runs")  # the moderator hasn't started the session

    response = submit(client)

    verdict = checkpoint_verdict(response)
    assert response.status_code == 200
    assert verdict["verdict"] == "failed"
    assert verdict["rejections"][0] == {
        "code": "session_not_started",
        "message": "The session hasn't started yet.",
    }
    assert referee.calls == []
    [row] = rows(db)
    assert (row["verdict"], row["rejections"][0]["code"]) == ("failed", "session_not_started")


def test_referee_runs_outside_the_write_transaction(
    client: TestClient, referee: FakeReferee
) -> None:
    submit(client)

    assert referee.lock_free_during_call == [True]


def test_lock_probe_detects_a_held_transaction(store: SubmissionStore, db: Db) -> None:
    with store.transaction(UUID(SESSION)):
        assert write_lock_is_free(db) is False
    assert write_lock_is_free(db) is True


def test_no_challenge_skips_the_referee(client: TestClient, referee: FakeReferee) -> None:
    response = submit(client, checkpoint=2)

    assert response.status_code == 202
    assert checkpoint_verdict(response)["verdict"] == "pending"
    assert referee.calls == []
    assert outcomes(response)["scene_matches"] == "skipped"


def test_no_api_key_is_pending_without_any_api_call(
    client: TestClient, mocker: MockerFixture, db: Db, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="game_server")
    wrapper = mocker.patch("game_server.referee._create_structured_message")
    client.app.dependency_overrides.pop(get_referee)  # type: ignore[attr-defined]  # FastAPI app

    response = submit(client)

    assert response.status_code == 202
    assert checkpoint_verdict(response)["verdict"] == "pending"
    assert outcomes(response)["scene_matches"] == "skipped"
    assert outcomes(response)["pose_correct"] == "skipped"
    wrapper.assert_not_called()
    [row] = rows(db)
    assert row["referee_status"] == "disabled"
    assert "referee disabled verdict pending" in caplog.text


# --- the model's reasons stay server-side -------------------------------------------


@pytest.mark.parametrize(
    ("scene_verdict", "confidence"),
    [("pass", 0.95), ("fail", 0.95), ("unsure", 0.95), ("pass", 0.3)],
)
def test_model_reasons_never_reach_response_or_log(
    client: TestClient,
    referee: FakeReferee,
    caplog: pytest.LogCaptureFixture,
    db: Db,
    scene_verdict: Verdict,
    confidence: float,
) -> None:
    referee.report = report(scene_verdict, "pass", confidence=confidence)
    caplog.set_level(logging.DEBUG)

    response = submit(client)

    for sentinel in ("SENTINEL-SCENE-REASON", "SENTINEL-POSE-REASON"):
        assert sentinel not in response.text
        assert sentinel not in caplog.text
    [row] = rows(db)
    assert {c["check"]: c["detail"] for c in row["checks"]}["scene_matches"] == SCENE_REASON
    assert row["referee_judgement"]["scene_matches"]["reason"] == SCENE_REASON


def test_row_records_the_referee_audit(
    client: TestClient, db: Db, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="game_server")

    submit(client)

    [row] = rows(db)
    assert row["verdict"] == "pass"
    assert (row["referee_status"], row["referee_model"], row["referee_error"]) == (
        "ok",
        "claude-haiku-4-5",
        None,
    )
    assert (row["referee_input_tokens"], row["referee_output_tokens"]) == (1600, 140)
    assert row["referee_latency_ms"] == 1234
    judgement = RefereeJudgement.model_validate(row["referee_judgement"])
    assert judgement.scene_matches.confidence == 0.95
    assert (
        "referee ok model=claude-haiku-4-5 latency_ms=1234 tokens=1600/140 verdict pass"
        in caplog.text
    )

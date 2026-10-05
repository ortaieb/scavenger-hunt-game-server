"""POST /challenge with the referee in the verdict (#22), using a fake Referee."""

import hashlib
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import anthropic
import httpx2
import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from images import jpeg, scene
from psycopg.rows import DictRow
from pytest_mock import MockerFixture

from game_server.app import create_app
from game_server.clock import get_clock, get_timer
from game_server.config import Settings, get_settings
from game_server.imaging import UndecodableImageError
from game_server.referee import (
    ClaudeReferee,
    ModelReply,
    RefereeCall,
    RefereeJudgement,
    RefereeReport,
    SentImage,
    VisualCheckJudgement,
    get_referee,
    prepare_image,
    system_prompt,
    user_text,
)
from game_server.sessions import VisualChallenge, get_session_repository, parse_sessions
from game_server.submissions import SubmissionStore

SESSION = "aeffe667-4f9f-4108-b5e2-56ae821fe413"
NOW = datetime(2026, 10, 3, 10, 30, tzinfo=UTC)
PHOTO = jpeg(scene(11))
SCENE_REASON = "SENTINEL-SCENE-REASON granite fountain ring behind the player"
POSE_REASON = "SENTINEL-POSE-REASON facing left in profile"
CHALLENGE = {"scene": "A granite fountain in open lawn", "pose": "Side profile"}
CALL = RefereeCall(
    system_prompt(),
    user_text(VisualChallenge(**CHALLENGE)),
    SentImage("ab" * 32, 640, 480),
    stop_reason="end_turn",
    response_text="SENTINEL-RAW-RESPONSE",
)
REFEREE_SECONDS = 2.5  # how long the fake referee's call takes, on the fake monotonic clock

PARTICIPANT = "7c860ccc-9adf-4e22-b54f-3ff158f5d600"
METADATA: dict[str, Any] = {
    "session": SESSION,
    "participant": PARTICIPANT,
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
        cost_usd=Decimal("0.0023"),
        call=CALL,
    )


Db = psycopg.Connection[DictRow]


def write_lock_is_free(db: Db) -> bool:
    """True if no write transaction holds a session's advisory lock."""
    row = db.execute(
        "SELECT NOT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory') AS free"
    ).fetchone()
    return row is not None and row["free"] is True


@dataclass
class FakeTimer:
    """A monotonic clock that only moves when the fake referee takes its time."""

    now: float = 100.0

    def __call__(self) -> float:
        return self.now


@dataclass
class FakeReferee:
    report: RefereeReport
    db: Db
    timer: FakeTimer
    calls: list[VisualChallenge] = field(default_factory=list)
    lock_free_during_call: list[bool] = field(default_factory=list)

    def judge(self, image: bytes, challenge: VisualChallenge) -> RefereeReport:
        self.calls.append(challenge)
        self.lock_free_during_call.append(write_lock_is_free(self.db))
        self.timer.now += REFEREE_SECONDS
        return self.report


@pytest.fixture
def timer() -> FakeTimer:
    return FakeTimer()


@pytest.fixture
def referee(db: Db, timer: FakeTimer) -> FakeReferee:
    return FakeReferee(report(), db, timer)


@pytest.fixture
def client(
    tmp_path: Path, referee: FakeReferee, timer: FakeTimer, store: SubmissionStore
) -> Iterator[TestClient]:
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
                    "teams": [{"name": "Red Foxes", "join-code": "FOX-7Q2K", "order": [1, 2]}],
                }
            ]
        )
    )
    settings = Settings(image_base_path=tmp_path / "images")
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: repository
    app.dependency_overrides[get_clock] = lambda: lambda: NOW
    app.dependency_overrides[get_timer] = lambda: timer
    app.dependency_overrides[get_referee] = lambda: referee
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def arrived(isolated_storage: None, db: Db, store: SubmissionStore) -> None:
    """The team has joined and checked in at checkpoint 1, so photos there are judged."""
    db.execute(
        "INSERT INTO participants (id, session, team, joined_at, consented_at)"
        " VALUES (%s, %s, 'Red Foxes', %s, %s)",
        (PARTICIPANT, SESSION, NOW, NOW),
    )
    check_in(store, 1, CHALLENGE["pose"])


def check_in(store: SubmissionStore, checkpoint: int, pose: str | None) -> None:
    store.arrive(
        UUID(SESSION),
        UUID(PARTICIPANT),
        checkpoint,
        pose=pose,
        now=NOW,
        ttl=timedelta(minutes=10),
        new_code=lambda: "1234",
    )


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


def traces(db: Db) -> list[DictRow]:
    return db.execute("SELECT * FROM referee_traces ORDER BY id").fetchall()


# --- verdicts --------------------------------------------------------------------


def test_everything_passing_awards_pass(client: TestClient, referee: FakeReferee) -> None:
    response = submit(client)

    verdict = checkpoint_verdict(response)
    assert response.status_code == 200
    assert verdict["verdict"] == "pass"
    assert verdict["rejections"] == []
    assert len(verdict["checks"]) == 9
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
        status="error",
        error_code="timeout",
        model="claude-haiku-4-5",
        latency_ms=20000,
        call=RefereeCall(CALL.system_prompt, CALL.user_text, CALL.image),
    )

    response = submit(client)

    assert response.status_code == 202
    assert checkpoint_verdict(response)["verdict"] == "pending"
    assert outcomes(response)["scene_matches"] == "uncertain"
    assert outcomes(response)["pose_correct"] == "uncertain"
    [row] = rows(db)
    [trace] = traces(db)
    assert trace["submission_id"] == row["id"]
    assert (trace["status"], trace["error_code"]) == ("error", "timeout")
    assert trace["latency_ms"] == 20000
    assert (trace["response_text"], trace["judgement"]) == (None, None)


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
    assert len(rows(db)) == 1
    assert traces(db) == []


def test_a_photo_after_the_stop_is_stored_failed_without_the_referee(
    client: TestClient, referee: FakeReferee, store: SubmissionStore, db: Db
) -> None:
    store.stop_run(UUID(SESSION), datetime(2026, 10, 3, 10, 15, tzinfo=UTC))
    db.execute("DELETE FROM arrivals")  # arrive is refused once the session has stopped

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
    assert traces(db) == []


def test_a_photo_before_the_start_is_stored_failed_without_the_referee(
    client: TestClient, referee: FakeReferee, db: Db
) -> None:
    db.execute("TRUNCATE session_runs")  # the moderator hasn't started the session
    db.execute("DELETE FROM arrivals")  # so arrive is refused

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


def test_no_challenge_skips_the_referee(
    client: TestClient, referee: FakeReferee, store: SubmissionStore
) -> None:
    check_in(store, 2, None)

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
    details = {c["check"]: c["detail"] for c in row["checks"]}
    assert details["scene_matches"] == "referee disabled: no API key"
    assert traces(db) == []  # no call, so nothing to trace
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

    for sentinel in ("SENTINEL-SCENE-REASON", "SENTINEL-POSE-REASON", "SENTINEL-RAW-RESPONSE"):
        assert sentinel not in response.text
        assert sentinel not in caplog.text
    assert CHALLENGE["scene"] not in response.text
    assert CHALLENGE["scene"] not in caplog.text
    [row] = rows(db)
    assert {c["check"]: c["detail"] for c in row["checks"]}["scene_matches"] == SCENE_REASON
    [trace] = traces(db)
    assert trace["judgement"]["scene_matches"]["reason"] == SCENE_REASON
    assert trace["response_text"] == "SENTINEL-RAW-RESPONSE"
    assert CHALLENGE["scene"] in trace["user_text"]


def test_row_records_the_referee_audit(
    client: TestClient, db: Db, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="game_server")

    submit(client)

    [row] = rows(db)
    assert row["verdict"] == "pass"
    [trace] = traces(db)
    assert (trace["submission_id"], trace["session"]) == (row["id"], UUID(SESSION))
    assert trace["image_id"] == row["image_id"]
    assert (trace["status"], trace["model"], trace["error_code"]) == (
        "ok",
        "claude-haiku-4-5",
        None,
    )
    assert (trace["request_id"], trace["stop_reason"]) == ("req_fake", "end_turn")
    assert (trace["input_tokens"], trace["output_tokens"]) == (1600, 140)
    assert (trace["latency_ms"], trace["cost_usd"]) == (1234, Decimal("0.0023"))
    judgement = RefereeJudgement.model_validate(trace["judgement"])
    assert judgement.scene_matches.confidence == 0.95
    assert row["processing_ms"] == 2500  # receiving, the referee's call, recording
    assert (
        "referee ok model=claude-haiku-4-5 latency_ms=1234 tokens=1600/140 "
        "verdict pass processing_ms 2500 " in caplog.text
    )


# --- processing time, and the trace committing with its submission (#62) -------------


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"location": {"lat": 51.51, "long": -0.1}}, id="out-of-range"),
        pytest.param({"checkpoint": 2}, id="no-challenge"),
    ],
)
def test_a_submission_without_the_referee_records_its_processing_time(
    client: TestClient,
    referee: FakeReferee,
    store: SubmissionStore,
    db: Db,
    changes: dict[str, Any],
) -> None:
    check_in(store, 2, None)

    submit(client, **changes)

    assert referee.calls == []
    [row] = rows(db)
    assert row["processing_ms"] == 0  # the fake clock only moves during a referee call
    assert traces(db) == []


def test_a_failure_after_the_referee_call_records_neither(
    client: TestClient, referee: FakeReferee, db: Db, mocker: MockerFixture
) -> None:
    mocker.patch("game_server.challenge.blocked_by_phase", side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        submit(client)

    assert len(referee.calls) == 1
    assert rows(db) == []
    assert traces(db) == []
    assert db.execute("SELECT * FROM referee_prompts").fetchall() == []


# --- the trace of each real referee call (#62), with the SDK call mocked -------------

WRAPPER = "game_server.referee._create_structured_message"
MAX_EDGE = 640
SERVED_MODEL = "claude-haiku-4-5-20251001"  # a dated snapshot of the configured model
PROMPT_FILE_TEXT = files("game_server").joinpath("referee_prompt.md").read_text(encoding="utf-8")
GOOD_OUTPUT = json.dumps(
    {
        "scene_matches": {"reason": SCENE_REASON, "verdict": "pass", "confidence": 0.95},
        "pose_correct": {"reason": POSE_REASON, "verdict": "pass", "confidence": 0.95},
    }
)
REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def model_reply(
    text: str = GOOD_OUTPUT, stop_reason: str = "end_turn", model: str = SERVED_MODEL
) -> ModelReply:
    return ModelReply(
        stop_reason=stop_reason,
        text=text,
        model=model,
        request_id="req_live",
        input_tokens=1500,
        output_tokens=120,
    )


@pytest.fixture
def claude(client: TestClient) -> ClaudeReferee:
    """The real referee judges the photos sent to `client`; each test mocks its SDK call."""
    client_ = anthropic.Anthropic(api_key="test-key-not-used")
    real = ClaudeReferee(client_, "claude-haiku-4-5", max_image_edge=MAX_EDGE)
    client.app.dependency_overrides[get_referee] = lambda: real  # type: ignore[attr-defined]  # FastAPI app
    return real


def sent_image() -> tuple[str, int, int]:
    """The hash and size of the JPEG the referee sends for PHOTO."""
    prepared = prepare_image(PHOTO, MAX_EDGE)
    return hashlib.sha256(prepared.jpeg).hexdigest(), prepared.width, prepared.height


@pytest.mark.usefixtures("claude")
@pytest.mark.parametrize(
    ("reply", "status", "error_code"),
    [
        pytest.param(model_reply(), "ok", None, id="ok"),
        pytest.param(model_reply(stop_reason="refusal"), "error", "refusal", id="refusal"),
        pytest.param(model_reply(stop_reason="max_tokens"), "error", "max_tokens", id="max-tokens"),
        pytest.param(model_reply(text="not json"), "error", "invalid_output", id="invalid-output"),
    ],
)
def test_a_reply_is_traced_with_every_column(
    client: TestClient,
    mocker: MockerFixture,
    db: Db,
    reply: ModelReply,
    status: str,
    error_code: str | None,
) -> None:
    mocker.patch(WRAPPER, return_value=reply)

    submit(client)

    [row] = rows(db)
    [trace] = traces(db)
    image_sha256, width, height = sent_image()
    assert dict(trace) == {
        "id": trace["id"],
        "session": UUID(SESSION),
        "created_at": trace["created_at"],
        "submission_id": row["id"],
        "image_id": row["image_id"],
        "image_sha256": image_sha256,
        "image_width": width,
        "image_height": height,
        "reference_photos": [],
        "prompt_sha256": hashlib.sha256(PROMPT_FILE_TEXT.encode()).hexdigest(),
        "user_text": user_text(VisualChallenge(**CHALLENGE)),  # the scene and the issued pose
        "model": SERVED_MODEL,
        "request_id": "req_live",
        "status": status,
        "error_code": error_code,
        "stop_reason": reply.stop_reason,
        "response_text": reply.text,
        "judgement": json.loads(GOOD_OUTPUT) if status == "ok" else None,
        "input_tokens": 1500,
        "output_tokens": 120,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
        "cost_usd": Decimal("0.0021"),  # 1500 x $1 + 120 x $5 per million tokens
        "latency_ms": trace["latency_ms"],
    }
    assert trace["latency_ms"] >= 0
    [prompt] = db.execute("SELECT text FROM referee_prompts").fetchall()
    assert prompt["text"] == PROMPT_FILE_TEXT


@pytest.mark.usefixtures("claude")
@pytest.mark.parametrize(
    ("error", "code"),
    [
        pytest.param(anthropic.APITimeoutError(request=REQUEST), "timeout", id="timeout"),
        pytest.param(
            anthropic.InternalServerError(
                "boom", response=httpx2.Response(500, request=REQUEST), body=None
            ),
            "api_error",
            id="api-error",
        ),
    ],
)
def test_a_call_without_a_reply_is_traced(
    client: TestClient, mocker: MockerFixture, db: Db, error: Exception, code: str
) -> None:
    mocker.patch(WRAPPER, side_effect=error)

    submit(client)

    [trace] = traces(db)
    assert (trace["status"], trace["error_code"], trace["model"]) == (
        "error",
        code,
        "claude-haiku-4-5",
    )
    assert (trace["image_sha256"], trace["image_width"], trace["image_height"]) == sent_image()
    assert trace["user_text"] == user_text(VisualChallenge(**CHALLENGE))
    for column in (
        "request_id",
        "stop_reason",
        "response_text",
        "judgement",
        "input_tokens",
        "output_tokens",
        "cost_usd",
    ):
        assert trace[column] is None, column


@pytest.mark.usefixtures("claude")
def test_a_photo_the_referee_cannot_prepare_is_traced_without_an_image(
    client: TestClient, mocker: MockerFixture, db: Db
) -> None:
    mocker.patch("game_server.referee.prepare_image", side_effect=UndecodableImageError)
    wrapper = mocker.patch(WRAPPER)

    submit(client)

    wrapper.assert_not_called()
    [trace] = traces(db)
    assert (trace["status"], trace["error_code"]) == ("error", "invalid_image")
    assert (trace["image_sha256"], trace["image_width"], trace["image_height"]) == (None,) * 3
    assert trace["prompt_sha256"] == hashlib.sha256(PROMPT_FILE_TEXT.encode()).hexdigest()


@pytest.mark.usefixtures("claude")
def test_a_changed_prompt_gets_its_own_hash_and_row(
    client: TestClient, mocker: MockerFixture, store: SubmissionStore, db: Db
) -> None:
    rejected = GOOD_OUTPUT.replace('"pass"', '"fail"')  # failed: the photo can be sent again
    mocker.patch(WRAPPER, return_value=model_reply(text=rejected))
    submit(client)
    mocker.patch("game_server.referee.system_prompt", return_value="REVISED PROMPT")
    check_in(store, 1, CHALLENGE["pose"])  # a fresh check-in: the first photo used its own

    submit(client)

    first, second = traces(db)
    assert first["prompt_sha256"] == hashlib.sha256(PROMPT_FILE_TEXT.encode()).hexdigest()
    assert second["prompt_sha256"] == hashlib.sha256(b"REVISED PROMPT").hexdigest()
    texts = {row["text"] for row in db.execute("SELECT text FROM referee_prompts")}
    assert texts == {PROMPT_FILE_TEXT, "REVISED PROMPT"}


@pytest.mark.usefixtures("claude")
def test_an_unknown_model_is_traced_without_a_cost_and_logged(
    client: TestClient, mocker: MockerFixture, db: Db, caplog: pytest.LogCaptureFixture
) -> None:
    mocker.patch(WRAPPER, return_value=model_reply(model="claude-future-9"))
    caplog.set_level(logging.WARNING, logger="game_server.referee")

    submit(client)

    [trace] = traces(db)
    assert (trace["model"], trace["cost_usd"]) == ("claude-future-9", None)
    assert "referee model=claude-future-9 has no price" in caplog.text

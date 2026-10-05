"""The referee's traces, recorded with their submission by `SubmissionStore.record`."""

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import DictRow

from game_server.referee import (
    RefereeCall,
    RefereeJudgement,
    RefereeReport,
    SentImage,
    VisualCheckJudgement,
)
from game_server.submissions import NewSubmission, SubmissionStore

Db = psycopg.Connection[DictRow]

SESSION = UUID(int=1)
IMAGE_ID = UUID(int=3)
PROMPT = "You are the referee."
PROMPT_SHA256 = hashlib.sha256(PROMPT.encode()).hexdigest()
SUBMISSION = NewSubmission(
    session=SESSION,
    participant=UUID(int=2),
    checkpoint=1,
    received_at=datetime(2026, 10, 3, 9, 30, tzinfo=UTC),
    capture_time=datetime(2026, 10, 3, 9, 29, tzinfo=UTC),
    lat=51.5,
    long=-0.1,
    image_id=IMAGE_ID,
    verdict="pending",
    checks=(),
    distance_m=12.5,
    phash=1,
    processing_ms=2400,
)
RULING = VisualCheckJudgement(reason="A fountain.", verdict="pass", confidence=0.9)
JUDGEMENT = RefereeJudgement(scene_matches=RULING, pose_correct=RULING)
SENT = SentImage(sha256="ab" * 32, width=1568, height=1045)
CALL = RefereeCall(PROMPT, "<scene>A fountain</scene>", SENT)
ANSWERED = replace(CALL, stop_reason="end_turn", response_text='{"scene_matches": ...}')
OK = RefereeReport(
    status="ok",
    judgement=JUDGEMENT,
    model="claude-haiku-4-5-20251001",
    request_id="req_123",
    input_tokens=1500,
    output_tokens=120,
    latency_ms=950,
    cost_usd=Decimal("0.0021"),
    call=ANSWERED,
)


def traces(db: Db) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute("SELECT * FROM referee_traces ORDER BY id")]


def prompts(db: Db) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute("SELECT * FROM referee_prompts ORDER BY sha256")]


def submission_ids(db: Db) -> list[int]:
    return [row["id"] for row in db.execute("SELECT id FROM submissions ORDER BY id")]


def test_an_ok_call_records_every_column(store: SubmissionStore, db: Db) -> None:
    before = datetime.now(UTC)

    recorded = store.record(replace(SUBMISSION, referee=OK))

    [trace] = traces(db)
    assert before <= trace["created_at"] <= datetime.now(UTC)
    assert trace == {
        "id": trace["id"],
        "session": SESSION,
        "created_at": trace["created_at"],
        "submission_id": recorded.id,
        "image_id": IMAGE_ID,  # the submission's own photo
        "image_sha256": SENT.sha256,
        "image_width": 1568,
        "image_height": 1045,
        "reference_photos": [],
        "prompt_sha256": PROMPT_SHA256,
        "user_text": "<scene>A fountain</scene>",
        "model": "claude-haiku-4-5-20251001",
        "request_id": "req_123",
        "status": "ok",
        "error_code": None,
        "stop_reason": "end_turn",
        "response_text": '{"scene_matches": ...}',
        "judgement": JUDGEMENT.model_dump(mode="json"),
        "input_tokens": 1500,
        "output_tokens": 120,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
        "cost_usd": Decimal("0.0021"),
        "latency_ms": 950,
    }


@pytest.mark.parametrize(
    ("call", "image_columns"),
    [
        pytest.param(CALL, (SENT.sha256, 1568, 1045), id="timeout"),
        pytest.param(RefereeCall(PROMPT, "text"), (None, None, None), id="invalid-image"),
    ],
)
def test_a_call_without_a_reply_is_traced(
    store: SubmissionStore,
    db: Db,
    call: RefereeCall,
    image_columns: tuple[object, ...],
) -> None:
    report = RefereeReport(
        status="error", error_code="timeout", model="claude-haiku-4-5", latency_ms=20000, call=call
    )

    store.record(replace(SUBMISSION, referee=report))

    [trace] = traces(db)
    assert (trace["status"], trace["error_code"], trace["latency_ms"]) == (
        "error",
        "timeout",
        20000,
    )
    assert (trace["image_sha256"], trace["image_width"], trace["image_height"]) == image_columns
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


def test_a_refused_call_keeps_the_raw_response(store: SubmissionStore, db: Db) -> None:
    refused = replace(
        OK,
        status="error",
        error_code="refusal",
        judgement=None,
        call=replace(ANSWERED, stop_reason="refusal", response_text="I can't help"),
    )

    store.record(replace(SUBMISSION, referee=refused))

    [trace] = traces(db)
    assert (trace["status"], trace["error_code"], trace["stop_reason"]) == (
        "error",
        "refusal",
        "refusal",
    )
    assert trace["response_text"] == "I can't help"
    assert trace["judgement"] is None
    assert trace["cost_usd"] == Decimal("0.0021")  # a refusal is still billed


@pytest.mark.parametrize(
    "report",
    [None, RefereeReport(status="disabled")],
    ids=["not-consulted", "disabled"],
)
def test_no_call_no_trace(store: SubmissionStore, db: Db, report: RefereeReport | None) -> None:
    store.record(replace(SUBMISSION, referee=report))

    assert len(submission_ids(db)) == 1
    assert traces(db) == []
    assert prompts(db) == []


def test_each_prompt_is_stored_once(store: SubmissionStore, db: Db) -> None:
    store.record(replace(SUBMISSION, referee=OK))
    store.record(replace(SUBMISSION, referee=OK))

    assert [trace["prompt_sha256"] for trace in traces(db)] == [PROMPT_SHA256] * 2
    [prompt] = prompts(db)
    assert (prompt["sha256"], prompt["text"]) == (PROMPT_SHA256, PROMPT)
    assert prompt["first_used_at"] is not None


def test_a_changed_prompt_is_stored_under_its_own_hash(store: SubmissionStore, db: Db) -> None:
    revised = "You are the referee. Judge the pose too."
    store.record(replace(SUBMISSION, referee=OK))

    store.record(replace(SUBMISSION, referee=replace(OK, call=RefereeCall(revised, "text"))))

    first, second = traces(db)
    assert first["prompt_sha256"] == PROMPT_SHA256
    assert second["prompt_sha256"] == hashlib.sha256(revised.encode()).hexdigest()
    assert {prompt["text"] for prompt in prompts(db)} == {PROMPT, revised}


def test_the_trace_rolls_back_with_its_submission(store: SubmissionStore, db: Db) -> None:
    with pytest.raises(RuntimeError), store.transaction(SESSION) as transaction:
        transaction.record(replace(SUBMISSION, referee=OK))
        raise RuntimeError("the blocked attempt could not be recorded")

    assert submission_ids(db) == []
    assert traces(db) == []
    assert prompts(db) == []


def test_a_trace_that_cannot_be_written_rolls_the_submission_back(
    store: SubmissionStore, db: Db
) -> None:
    broken = replace(OK, model=None)  # the trace's model is NOT NULL

    with pytest.raises(psycopg.errors.NotNullViolation):
        store.record(replace(SUBMISSION, referee=broken))

    assert submission_ids(db) == []
    assert prompts(db) == []


def test_deleting_the_submission_deletes_its_trace(store: SubmissionStore, db: Db) -> None:
    store.record(replace(SUBMISSION, referee=OK))

    db.execute("DELETE FROM submissions WHERE session = %s", (SESSION,))

    assert traces(db) == []
    assert len(prompts(db)) == 1  # prompts aren't session data


def test_traces_are_found_by_session(store: SubmissionStore, db: Db) -> None:
    store.record(replace(SUBMISSION, referee=OK))
    store.record(replace(SUBMISSION, session=UUID(int=9), referee=OK))

    rows = db.execute("SELECT session FROM referee_traces WHERE session = %s", (SESSION,))

    assert [row["session"] for row in rows] == [SESSION]

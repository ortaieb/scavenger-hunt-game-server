from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import DictRow

from game_server.checks import CheckResult, Rejection
from game_server.config import Settings
from game_server.database import Database, database_config, open_database
from game_server.referee import RefereeJudgement, RefereeReport, VisualCheckJudgement
from game_server.submissions import NewSubmission, SubmissionStore, get_submission_store

SESSION = UUID(int=1)
PARTICIPANT = UUID(int=2)

SUBMISSION = NewSubmission(
    session=SESSION,
    participant=PARTICIPANT,
    checkpoint=1,
    received_at=datetime(2026, 10, 3, 9, 30, tzinfo=UTC),
    capture_time=datetime(2026, 10, 3, 10, 29, tzinfo=timezone(timedelta(hours=1))),
    lat=51.5,
    long=-0.1,
    image_id=UUID(int=3),
    verdict="pending",
    checks=(),
    distance_m=12.5,
    phash=0xFEDC_BA98_7654_3210,
)


def rows(db: psycopg.Connection[DictRow]) -> list[DictRow]:
    return db.execute("SELECT * FROM submissions ORDER BY id").fetchall()


def test_records_all_fields(store: SubmissionStore, db: psycopg.Connection[DictRow]) -> None:
    submission = replace(
        SUBMISSION,
        verdict="failed",
        checks=(
            CheckResult.failed("window_open", Rejection("outside_window", "Closed")),
            CheckResult("scene_matches", "uncertain", 0.5, "Unsure.", detail="blurry"),
            CheckResult.failed("b_ok", Rejection("b", "B")),
        ),
    )

    recorded = store.record(submission)

    [row] = rows(db)
    assert row["id"] == recorded.id
    assert (row["session"], row["participant"]) == (SESSION, PARTICIPANT)
    assert (row["checkpoint"], row["attempt"]) == (1, 1)
    assert row["received_at"] == datetime(2026, 10, 3, 9, 30, tzinfo=UTC)
    assert row["capture_time"] == datetime(2026, 10, 3, 9, 29, tzinfo=UTC)  # the same instant
    assert row["received_at"].utcoffset() == timedelta(0)
    assert (row["lat"], row["long"]) == (51.5, -0.1)
    assert row["image_id"] == UUID(int=3)
    assert row["verdict"] == "failed"
    assert row["distance_m"] == 12.5
    assert row["phash"] == "fedcba9876543210"
    assert row["rejections"] == [
        {"code": "outside_window", "message": "Closed"},
        {"code": "b", "message": "B"},
    ]
    assert row["checks"][1] == {
        "check": "scene_matches",
        "outcome": "uncertain",
        "confidence": 0.5,
        "reason": "Unsure.",
        "detail": "blurry",
    }
    assert row["referee_status"] is None
    assert row["referee_judgement"] is None


def test_records_the_referee_report(
    store: SubmissionStore, db: psycopg.Connection[DictRow]
) -> None:
    ruling = VisualCheckJudgement(reason="A fountain.", verdict="pass", confidence=0.9)
    report = RefereeReport(
        status="ok",
        judgement=RefereeJudgement(scene_matches=ruling, pose_correct=ruling),
        model="claude-haiku-4-5",
        input_tokens=1200,
        output_tokens=80,
        latency_ms=950,
    )

    store.record(replace(SUBMISSION, referee=report))

    [row] = rows(db)
    assert (row["referee_status"], row["referee_model"], row["referee_error"]) == (
        "ok",
        "claude-haiku-4-5",
        None,
    )
    assert row["referee_judgement"]["scene_matches"] == {
        "reason": "A fountain.",
        "verdict": "pass",
        "confidence": 0.9,
    }
    assert (
        row["referee_input_tokens"],
        row["referee_output_tokens"],
        row["referee_latency_ms"],
    ) == (1200, 80, 950)


def test_attempts_are_numbered_per_session_participant_and_checkpoint(
    store: SubmissionStore,
) -> None:
    other_session = replace(SUBMISSION, session=uuid4())
    other_participant = replace(SUBMISSION, participant=uuid4())
    other_checkpoint = replace(SUBMISSION, checkpoint=2)
    sequence = [
        SUBMISSION,
        SUBMISSION,
        other_session,
        other_participant,
        other_checkpoint,
        other_checkpoint,
        SUBMISSION,
    ]

    attempts = [store.record(submission).attempt for submission in sequence]

    assert attempts == [1, 2, 1, 1, 1, 2, 3]


def test_concurrent_submissions_get_distinct_attempts(store: SubmissionStore) -> None:
    count = 20

    with ThreadPoolExecutor(max_workers=8) as pool:
        attempts = list(pool.map(lambda _: store.record(SUBMISSION).attempt, range(count)))

    assert sorted(attempts) == list(range(1, count + 1))


def test_duplicate_attempt_is_refused_by_the_database(
    store: SubmissionStore, db: psycopg.Connection[DictRow]
) -> None:
    store.record(SUBMISSION)

    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(
            "INSERT INTO submissions (session, participant, checkpoint, attempt, received_at,"
            " capture_time, lat, long, image_id, verdict, rejections, distance_m, phash, checks)"
            " VALUES (%s, %s, 1, 1, now(), now(), 0, 0, %s, 'pending', '[]', 0, '0', '[]')",
            (SESSION, PARTICIPANT, uuid4()),
        )


def test_failed_insert_is_rolled_back(
    store: SubmissionStore, db: psycopg.Connection[DictRow]
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        store.record(replace(SUBMISSION, verdict="bogus"))  # type: ignore[arg-type]  # CHECK

    assert rows(db) == []
    assert store.record(SUBMISSION).attempt == 1


def test_transaction_rolls_back_on_any_exception(
    store: SubmissionStore, db: psycopg.Connection[DictRow]
) -> None:
    with pytest.raises(RuntimeError), store.transaction(SESSION) as transaction:
        transaction.record(SUBMISSION)
        raise RuntimeError("the image could not be saved")

    assert rows(db) == []


def test_transaction_serialises_its_session_only(store: SubmissionStore) -> None:
    """A session's lock holds back that session's writes, not another session's."""
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        with store.transaction(SESSION):
            same = pool.submit(store.record, SUBMISSION)
            other = pool.submit(store.record, replace(SUBMISSION, session=uuid4()))
            assert other.result(timeout=5).attempt == 1  # not held back
            with pytest.raises(TimeoutError):
                same.result(timeout=0.3)  # waits for the session's lock
        assert same.result(timeout=5).attempt == 1
    finally:
        pool.shutdown()


def test_dependency_uses_the_configured_database(db: psycopg.Connection[DictRow]) -> None:
    database = open_database(database_config(Settings()))

    get_submission_store(database).record(SUBMISSION)

    assert len(rows(db)) == 1


def test_stores_share_the_pool(database: Database) -> None:
    SubmissionStore(database).record(SUBMISSION)

    assert SubmissionStore(database).record(SUBMISSION).attempt == 2

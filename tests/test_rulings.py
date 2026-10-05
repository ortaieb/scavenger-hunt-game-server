"""The moderator's rulings as stored, and what follows from them (`ruled_submissions`)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import DictRow

from game_server.models import VerdictStatus
from game_server.rulings import Ruling, StoredRuling
from game_server.submissions import NewSubmission, SubmissionStore

SESSION = UUID(int=1)
OTHER_SESSION = UUID(int=9)
PARTICIPANT = UUID(int=2)
RECEIVED = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
RULED = datetime(2026, 10, 3, 10, 30, tzinfo=UTC)

SUBMISSION = NewSubmission(
    session=SESSION,
    participant=PARTICIPANT,
    checkpoint=1,
    received_at=RECEIVED,
    capture_time=RECEIVED,
    lat=51.5,
    long=-0.1,
    image_id=UUID(int=3),
    verdict="pending",
    checks=(),
    distance_m=1.0,
    phash=0,
    processing_ms=40,
)


def record(
    store: SubmissionStore,
    verdict: VerdictStatus = "pending",
    *,
    session: UUID = SESSION,
    participant: UUID = PARTICIPANT,
    checkpoint: int = 1,
    phash: int = 0,
) -> int:
    submission = replace(
        SUBMISSION,
        session=session,
        participant=participant,
        checkpoint=checkpoint,
        verdict=verdict,
        phash=phash,
    )
    return store.record(submission).id


def rule(
    store: SubmissionStore, submission: int, ruling: Ruling, note: str | None = None
) -> VerdictStatus:
    recorded = store.rule(SESSION, submission, ruling, note, RULED)
    assert recorded is not None
    return recorded.effective_verdict


# --- the effective verdict -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "ruling", "effective"),
    [
        ("pending", "approve", "pass"),
        ("pending", "reject", "failed"),
        ("pass", "approve", "pass"),
        ("pass", "reject", "failed"),
        ("failed", "approve", "pass"),
        ("failed", "reject", "failed"),
    ],
)
def test_approve_is_pass_and_reject_is_failed_whatever_the_verdict(
    store: SubmissionStore, verdict: VerdictStatus, ruling: Ruling, effective: VerdictStatus
) -> None:
    submission = record(store, verdict)

    assert rule(store, submission, ruling) == effective


def test_the_latest_ruling_wins(store: SubmissionStore) -> None:
    submission = record(store)

    assert rule(store, submission, "approve") == "pass"
    assert rule(store, submission, "reject") == "failed"
    assert rule(store, submission, "approve") == "pass"


def test_the_first_ruling_is_marked_first(store: SubmissionStore) -> None:
    submission = record(store)

    first = store.rule(SESSION, submission, "approve", "cropped arm", RULED)
    second = store.rule(SESSION, submission, "reject", None, RULED + timedelta(minutes=1))

    assert first is not None and second is not None
    assert (first.first, second.first) == (True, False)
    assert first.ruling == StoredRuling("approve", "cropped arm", RULED)
    assert second.ruling == StoredRuling("reject", None, RULED + timedelta(minutes=1))
    assert first.verdict == second.verdict == "pending"


def test_every_ruling_is_kept(store: SubmissionStore, db: psycopg.Connection[DictRow]) -> None:
    submission = record(store)
    rule(store, submission, "approve", "first look")
    rule(store, submission, "reject")

    rows = db.execute("SELECT session, submission_id, ruling, note FROM rulings ORDER BY id")

    assert rows.fetchall() == [
        {
            "session": SESSION,
            "submission_id": submission,
            "ruling": "approve",
            "note": "first look",
        },
        {"session": SESSION, "submission_id": submission, "ruling": "reject", "note": None},
    ]


def test_a_ruling_never_changes_the_submission(
    store: SubmissionStore, db: psycopg.Connection[DictRow]
) -> None:
    submission = record(store)
    before = db.execute("SELECT * FROM submissions").fetchall()

    rule(store, submission, "reject")

    assert db.execute("SELECT * FROM submissions").fetchall() == before


@pytest.mark.parametrize("session", [OTHER_SESSION, SESSION], ids=["other-session", "unknown"])
def test_a_submission_not_in_the_session_is_not_ruled(
    store: SubmissionStore, db: psycopg.Connection[DictRow], session: UUID
) -> None:
    other = record(store, session=OTHER_SESSION)
    submission = other if session == OTHER_SESSION else other + 1

    assert store.rule(SESSION, submission, "approve", None, RULED) is None
    assert db.execute("SELECT * FROM rulings").fetchall() == []


def test_rulings_go_with_their_submission(
    store: SubmissionStore, db: psycopg.Connection[DictRow]
) -> None:
    rule(store, record(store), "approve")

    db.execute("DELETE FROM submissions")

    assert db.execute("SELECT * FROM rulings").fetchall() == []


# --- progress --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "rulings", "completes"),
    [
        ("pass", [], True),
        ("pending", [], True),
        ("failed", [], False),
        ("pending", ["reject"], True),
        ("pass", ["reject"], True),
        ("failed", ["approve"], True),
        ("failed", ["reject"], False),
        ("failed", ["approve", "reject"], True),
        ("failed", ["reject", "approve"], True),
    ],
    ids=[
        "pass",
        "pending",
        "failed",
        "rejected-pending",
        "rejected-pass",
        "approved-failed",
        "rejected-failed",
        "approved-then-rejected",
        "rejected-then-approved",
    ],
)
def test_a_ruling_never_takes_a_checkpoint_back(
    store: SubmissionStore, verdict: VerdictStatus, rulings: list[Ruling], completes: bool
) -> None:
    submission = record(store, verdict)
    for ruling in rulings:
        rule(store, submission, ruling)

    completed = store.completed_checkpoints(SESSION, PARTICIPANT)

    assert completed == (frozenset({1}) if completes else frozenset())


def test_completing_submissions_show_the_effective_verdict(store: SubmissionStore) -> None:
    team = store.join_team(SESSION, "Red Foxes", RECEIVED).participant
    rejected = record(store, "pending", participant=team)
    approved = record(store, "failed", participant=team, checkpoint=2)
    record(store, "failed", participant=team, checkpoint=3)
    rule(store, rejected, "reject")
    rule(store, approved, "approve")

    shown = store.completing_submissions(SESSION)

    assert [(s.checkpoint, s.verdict) for s in shown] == [(1, "failed"), (2, "pass")]


# --- scoring and the duplicate check -------------------------------------------------------


def test_results_follow_the_effective_verdict(store: SubmissionStore) -> None:
    team = store.join_team(SESSION, "Red Foxes", RECEIVED).participant
    approved = record(store, "pending", participant=team)
    rejected = record(store, "pass", participant=team, checkpoint=2)
    record(store, "pending", participant=team, checkpoint=3)
    rule(store, approved, "approve")
    rule(store, rejected, "reject")

    results = store.session_results(SESSION)

    assert results.passes == {("Red Foxes", 1): RECEIVED}
    assert results.pending == frozenset({("Red Foxes", 3)})
    assert results.to_review == 1


def test_to_review_counts_the_sessions_unruled_pending_photos(store: SubmissionStore) -> None:
    ruled = record(store, "pending")
    record(store, "pending", checkpoint=2)  # a participant that never joined still counts
    record(store, "pending", checkpoint=3)
    record(store, "pass", checkpoint=4)
    record(store, "pending", session=OTHER_SESSION)
    rule(store, ruled, "approve")

    assert store.session_results(SESSION).to_review == 2


def test_the_duplicate_check_compares_photos_whose_effective_verdict_is_not_failed(
    store: SubmissionStore,
) -> None:
    rejected = record(store, "pass", phash=1)
    approved = record(store, "failed", phash=2)
    record(store, "failed", phash=3)
    kept = record(store, "pending", phash=4)
    rule(store, rejected, "reject")
    rule(store, approved, "approve")

    with store.transaction(SESSION) as transaction:
        accepted = transaction.accepted_photos(SESSION)

    assert [photo.submission_id for photo in accepted] == [approved, kept]

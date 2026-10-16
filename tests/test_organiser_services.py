"""The organiser's services: start a design, publish a draft, rule on a photo. No FastAPI."""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from pytest_mock import MockerFixture
from test_designer_review import (
    FILE_CODE,
    FILE_SESSION,
    HUNT,
    NOW,
    FixedCodes,
    close_to_first,
    file_sessions,
    ready_draft,
)

from game_server.config import Settings
from game_server.database import Database
from game_server.designer.review import (
    CheckpointEdit,
    DraftNotReadyError,
    DraftProblemsError,
    PublishRequest,
    apply_edit,
)
from game_server.designer_runners import stub_checkpoints
from game_server.drafts import DesignerBusyError, DraftRequest, DraftStore
from game_server.organiser_services import (
    DesignerDisabledError,
    UnknownDraftError,
    publish_draft,
    rule,
    start_design,
)
from game_server.published_sessions import PublishedSessionRows
from game_server.rulings import RecordedRuling, Ruling, StoredRuling
from game_server.sessions import GameSession, SessionPublishError, SessionRepository
from game_server.submissions import SubmissionStore

DESIGN = DraftRequest.model_validate({"area": "Chiswick, London", "theme": "Brewing"})


def clock() -> datetime:
    return NOW


@dataclass
class FakeRunner:
    """Records the runs it was asked to start."""

    ready: bool = True
    name: str = "fake"
    started: list[tuple[UUID, DraftRequest]] = field(default_factory=list)

    def available(self) -> bool:
        return self.ready

    def start(self, draft_id: UUID, request: DraftRequest) -> None:
        self.started.append((draft_id, request))


@pytest.fixture
def store(database: Database) -> DraftStore:
    return DraftStore(database)


@pytest.fixture
def codes() -> FixedCodes:
    return FixedCodes()


@pytest.fixture
def sessions(database: Database) -> SessionRepository:
    return file_sessions().beside(PublishedSessionRows(database))


def accept_all(store: DraftStore, draft: UUID) -> None:
    with store.locked(draft) as locked:
        assert locked is not None
        accept = CheckpointEdit(review="accepted")
        locked.save_checkpoints([apply_edit(c, accept) for c in locked.draft.checkpoints])


def publish(
    store: DraftStore, sessions: SessionRepository, codes: FixedCodes, draft: UUID
) -> GameSession:
    return publish_draft(
        store, draft, PublishRequest.model_validate(HUNT), Settings(), sessions, codes, clock
    )


# --- start_design --------------------------------------------------------------------------------


def test_a_design_is_stored_running_and_started(
    store: DraftStore, caplog: pytest.LogCaptureFixture
) -> None:
    runner = FakeRunner()
    caplog.set_level(logging.INFO)

    draft = start_design(store, runner, clock, DESIGN)

    found = store.get(draft)
    assert found is not None
    assert (found.status, found.runner) == ("running", "fake")
    assert runner.started == [(draft, DESIGN)]
    assert f"Draft {draft} created status running runner fake" in caplog.text


def test_a_design_is_refused_when_the_runner_is_unavailable(store: DraftStore) -> None:
    runner = FakeRunner(ready=False)

    with pytest.raises(DesignerDisabledError, match="not available"):
        start_design(store, runner, clock, DESIGN)

    assert runner.started == []
    assert store.list(10) == []


def test_a_second_design_while_one_runs_is_busy(store: DraftStore) -> None:
    runner = FakeRunner()
    start_design(store, runner, clock, DESIGN)

    with pytest.raises(DesignerBusyError):
        start_design(store, runner, clock, DESIGN)

    assert len(runner.started) == 1


# --- publish_draft -------------------------------------------------------------------------------


def test_publishing_marks_the_draft_and_logs_it(
    store: DraftStore,
    sessions: SessionRepository,
    codes: FixedCodes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    draft = ready_draft(store)
    accept_all(store, draft)
    caplog.set_level(logging.INFO)

    session = publish(store, sessions, codes, draft)

    found = store.get(draft)
    assert found is not None
    assert (found.status, found.published_session) == ("published", session.id)
    assert (len(session.teams), len(session.checkpoints)) == (2, 4)
    assert f"Draft {draft} published as session {session.id} teams 2 checkpoints 4" in caplog.text


def test_publishing_an_unknown_draft(
    store: DraftStore, sessions: SessionRepository, codes: FixedCodes
) -> None:
    with pytest.raises(UnknownDraftError):
        publish(store, sessions, codes, uuid4())


def test_publishing_a_draft_with_checkpoints_pending_review(
    store: DraftStore, sessions: SessionRepository, codes: FixedCodes
) -> None:
    draft = ready_draft(store)

    with pytest.raises(DraftNotReadyError, match="4 still pending review"):
        publish(store, sessions, codes, draft)


def test_publishing_twice(
    store: DraftStore, sessions: SessionRepository, codes: FixedCodes
) -> None:
    draft = ready_draft(store)
    accept_all(store, draft)
    publish(store, sessions, codes, draft)

    with pytest.raises(DraftNotReadyError, match="already published"):
        publish(store, sessions, codes, draft)


def test_publishing_checkpoints_that_break_the_rules(
    store: DraftStore, sessions: SessionRepository, codes: FixedCodes
) -> None:
    draft = ready_draft(store, (*stub_checkpoints(), close_to_first()))
    accept_all(store, draft)

    with pytest.raises(DraftProblemsError) as caught:
        publish(store, sessions, codes, draft)

    assert caught.value.problems


def test_a_code_taken_since_it_was_drawn_is_redrawn(
    store: DraftStore, sessions: SessionRepository
) -> None:
    draft = ready_draft(store)
    accept_all(store, draft)
    codes = FixedCodes(joins=[FILE_CODE, "FRESH-ONE-1"])

    session = publish(store, sessions, codes, draft)

    assert FILE_CODE not in {team.join_code for team in session.teams}


def test_a_session_the_repository_refuses_is_a_problem_of_the_draft(
    store: DraftStore, sessions: SessionRepository, codes: FixedCodes, mocker: MockerFixture
) -> None:
    draft = ready_draft(store)
    accept_all(store, draft)
    mocker.patch.object(sessions, "publish_session", side_effect=SessionPublishError(["x: bad"]))

    with pytest.raises(DraftProblemsError) as caught:
        publish(store, sessions, codes, draft)

    assert [(p.code, p.message) for p in caught.value.problems] == [("invalid_session", "x: bad")]


def test_giving_up_when_every_code_drawn_is_taken(
    store: DraftStore, sessions: SessionRepository, codes: FixedCodes, mocker: MockerFixture
) -> None:
    draft = ready_draft(store)
    accept_all(store, draft)
    taken = SessionPublishError(["teams[0].join-code: already in use"])
    mocker.patch.object(sessions, "publish_session", side_effect=taken)

    with pytest.raises(RuntimeError, match="no unused codes after 3 attempts"):
        publish(store, sessions, codes, draft)


# --- rule ----------------------------------------------------------------------------------------


@dataclass
class FakeSubmissions:
    """Answers `rule` with a canned result, recording the call."""

    recorded: RecordedRuling | None
    calls: list[tuple[UUID, int, Ruling, str | None, datetime]] = field(default_factory=list)

    def rule(
        self,
        session: UUID,
        submission: int,
        ruling: Ruling,
        note: str | None,
        now: datetime,
        pid: UUID | None = None,
    ) -> RecordedRuling | None:
        self.calls.append((session, submission, ruling, note, now))
        return self.recorded


@pytest.fixture
def session() -> GameSession:
    found = file_sessions().get_session(UUID(FILE_SESSION))
    assert found is not None
    return found


def test_a_ruling_is_recorded_and_logged_without_its_note(
    session: GameSession, caplog: pytest.LogCaptureFixture
) -> None:
    recorded = RecordedRuling(
        submission=7,
        verdict="failed",
        ruling=StoredRuling(ruling="approve", note="SENTINEL-NOTE", ruled_at=NOW),
        effective_verdict="pass",
        first=True,
    )
    fake = FakeSubmissions(recorded)
    caplog.set_level(logging.INFO)

    result = rule(cast(SubmissionStore, fake), session, 7, "approve", "SENTINEL-NOTE", clock)

    assert result is recorded
    assert fake.calls == [(session.id, 7, "approve", "SENTINEL-NOTE", NOW)]
    assert "submission 7 approve verdict failed -> pass first=yes" in caplog.text
    assert "SENTINEL-NOTE" not in caplog.text


def test_a_ruling_on_an_unknown_submission_is_none_and_not_logged(
    session: GameSession, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    assert (
        rule(cast(SubmissionStore, FakeSubmissions(None)), session, 7, "reject", None, clock)
        is None
    )
    assert "Moderator ruling" not in caplog.text

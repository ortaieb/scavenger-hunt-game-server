"""DraftStore: the hunt_drafts table."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

import pytest

from game_server.database import Database
from game_server.designer_runners import stub_area, stub_checkpoints
from game_server.drafts import (
    DesignerBusyError,
    DraftRequest,
    DraftResult,
    DraftStore,
    Problem,
    ProgressEntry,
    RunErrorCode,
)

T0 = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
REQUEST = DraftRequest.model_validate({"area": "Chiswick", "theme": "Brewing", "checkpoints": 4})


@pytest.fixture
def store(database: Database) -> DraftStore:
    return DraftStore(database)


def result(
    status: Literal["ready", "failed"] = "ready", error_code: RunErrorCode | None = None
) -> DraftResult:
    return DraftResult(
        status=status,
        area=stub_area(REQUEST),
        checkpoints=stub_checkpoints(),
        problems=(Problem(code="too_close", position=2, message="Too close to 1."),),
        model="claude-test",
        turns=12,
        cost_usd=Decimal("0.123456"),
        duration_ms=4321,
        error_code=error_code,
    )


def created(store: DraftStore, at: datetime = T0) -> UUID:
    draft = uuid4()
    store.create(draft, REQUEST, "stub", at)
    return draft


def test_a_new_draft_is_running(store: DraftStore) -> None:
    draft = created(store)

    found = store.get(draft)

    assert found is not None
    assert (found.status, found.request, found.runner, found.created_at) == (
        "running",
        REQUEST,
        "stub",
        T0,
    )
    assert (found.area, found.checkpoints, found.progress, found.problems) == (None, (), (), ())
    assert (found.turns, found.cost_usd, found.finished_at, found.error_code) == (
        0,
        Decimal(0),
        None,
        None,
    )


def test_a_finished_draft_reads_back_as_written(store: DraftStore) -> None:
    draft = created(store)

    assert store.finish(draft, result(), T0 + timedelta(seconds=5)) is True

    found = store.get(draft)
    assert found is not None
    assert found.status == "ready"
    assert found.area == stub_area(REQUEST)
    assert found.checkpoints == stub_checkpoints()
    assert found.problems == (Problem(code="too_close", position=2, message="Too close to 1."),)
    assert (found.model, found.turns, found.cost_usd, found.duration_ms) == (
        "claude-test",
        12,
        Decimal("0.123456"),
        4321,
    )
    assert found.finished_at == T0 + timedelta(seconds=5)


def test_only_one_draft_runs_at_a_time(store: DraftStore) -> None:
    created(store)

    with pytest.raises(DesignerBusyError):
        created(store)


@pytest.mark.parametrize("status", ["ready", "failed"])
def test_a_finished_draft_frees_the_designer(
    store: DraftStore, status: Literal["ready", "failed"]
) -> None:
    store.finish(created(store), result(status=status), T0)

    assert store.get(created(store)) is not None


def test_a_finished_draft_is_not_written_again(store: DraftStore) -> None:
    draft = created(store)
    store.finish(draft, result(), T0)

    again = store.finish(draft, result(status="failed", error_code="interrupted"), T0)

    found = store.get(draft)
    assert again is False
    assert found is not None
    assert (found.status, found.error_code) == ("ready", None)


def test_progress_is_appended_while_running(store: DraftStore) -> None:
    draft = created(store)
    first = ProgressEntry(at="2026-10-08T09:00:01Z", step="find_places", summary="58 places")
    second = ProgressEntry(at="2026-10-08T09:00:02Z", step="write_clues", summary="3 clues")

    store.append_progress(draft, first)
    store.append_progress(draft, second)
    store.finish(draft, result(), T0)
    store.append_progress(draft, first)  # too late: ignored

    found = store.get(draft)
    assert found is not None
    assert found.progress == (first, second)


def test_unknown_draft_is_none(store: DraftStore) -> None:
    assert store.get(uuid4()) is None


def test_the_list_summarises_newest_first(store: DraftStore) -> None:
    older = created(store, T0)
    store.finish(older, result(), T0 + timedelta(seconds=1))
    newer = created(store, T0 + timedelta(minutes=1))

    listed = store.list(10)

    assert [d.id for d in listed] == [newer, older]
    assert (listed[1].area, listed[1].theme, listed[1].checkpoints, listed[1].cost_usd) == (
        "Chiswick",
        "Brewing",
        3,
        Decimal("0.123456"),
    )
    assert (listed[0].status, listed[0].checkpoints, listed[0].finished_at) == ("running", 0, None)

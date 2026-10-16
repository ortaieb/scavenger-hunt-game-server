"""The MCP submission tools: list a session's submissions and read one, against seeded
submissions and rulings. No photos, coordinates, participant ids or codes."""

import json
from typing import Any
from uuid import UUID

import pytest
from mcp.types import TextContent
from mcp_fakes import call_tool, error_of, stub_services
from test_review import (
    CHECKED_IN,
    DEADLINE,
    DURING,
    FOX,
    HERON,
    LAT,
    LONG,
    MODERATOR,
    OK,
    OTHER,
    POSE,
    POSE_UNSURE,
    SCENE,
    SESSION,
    arrive,
    minutes,
    record,
    session_json,
)

from game_server.checks import CheckResult, Rejection
from game_server.config import Settings
from game_server.database import Database
from game_server.mcp_server.services import McpServices
from game_server.sessions import SessionRepository, parse_sessions
from game_server.submissions import SubmissionStore

WEB = "https://play.example/"
FAR = Rejection("out_of_range", "You're too far away.")
CLOSED = Rejection("outside_window", "This checkpoint is closed.")
OUT_OF_RANGE = CheckResult(
    "in_range", "failed", 1.0, FAR.message, rejection=FAR, detail="41 m from the checkpoint"
)
LATE = CheckResult("in_window", "failed", 1.0, CLOSED.message, rejection=CLOSED)


@pytest.fixture
def settings() -> Settings:
    return Settings(web_app_url=WEB)


@pytest.fixture
def repository() -> SessionRepository:
    sessions = [session_json(SESSION), session_json(OTHER, suffix="-O")]
    for session in sessions:
        for checkpoint in session["checkpoints"]:
            checkpoint.pop("reference-photos", None)
    return parse_sessions(json.dumps(sessions))


@pytest.fixture
def services(database: Database, repository: SessionRepository) -> McpServices:
    return stub_services(database, repository=repository)


class Seeded:
    """Seven submissions of one session (ids in time order), and one of another."""

    def __init__(self, store: SubmissionStore) -> None:
        self.store = store
        fox = store.join_team(UUID(SESSION), "Foxes", DURING).participant
        heron = store.join_team(UUID(SESSION), "Herons", DURING).participant
        self.fox, self.heron = fox, heron
        arrival = arrive(store, fox)
        # The referee passed it.
        self.passed = record(store, fox, verdict="pass", at=minutes(1), checks=(CHECKED_IN,))
        # Failed on range; a second attempt at the same checkpoint.
        self.far = record(
            store, fox, verdict="failed", at=minutes(2), checks=(CHECKED_IN, OUT_OF_RANGE, LATE)
        )
        # Undecided, nobody has ruled: the moderator's queue.
        self.waiting = record(
            store,
            heron,
            verdict="pending",
            at=minutes(3),
            checkpoint=2,
            checks=(CHECKED_IN, POSE_UNSURE),
            referee=OK,
        )
        # Undecided and approved.
        self.approved = record(
            store, heron, verdict="pending", at=minutes(4), checks=(POSE_UNSURE,), referee=OK
        )
        rule(store, self.approved, "approve", minutes(5), "Looks right")
        # Undecided, rejected and then approved: two rulings.
        self.changed = record(
            store,
            fox,
            verdict="pending",
            at=minutes(6),
            checkpoint=2,
            checks=(POSE_UNSURE,),
            referee=DEADLINE,
        )
        rule(store, self.changed, "reject", minutes(7), "Not the pose")
        rule(store, self.changed, "approve", minutes(8), None)
        # Failed by the referee, overturned.
        self.overturned = record(
            store,
            fox,
            verdict="failed",
            at=minutes(9),
            checks=(POSE_UNSURE,),
            referee=OK,
            arrival_id=arrival,
        )
        rule(store, self.overturned, "approve", minutes(10), "The pose was fine")
        self.elsewhere = record(store, uuid_of(store), session=OTHER, verdict="pending")


def uuid_of(store: SubmissionStore) -> UUID:
    return store.join_team(UUID(OTHER), "Foxes", DURING).participant


def rule(store: SubmissionStore, submission: int, ruling: str, at: Any, note: str | None) -> None:
    assert store.rule(UUID(SESSION), submission, ruling, note, at) is not None  # type: ignore[arg-type]


@pytest.fixture
def seeded(store: SubmissionStore) -> Seeded:
    return Seeded(store)


def listed(services: McpServices, settings: Settings, **arguments: Any) -> dict[str, Any]:
    result = call_tool(services, settings, "list_submissions", {"session_id": SESSION, **arguments})
    assert not result.is_error, result.content
    body: dict[str, Any] = result.structured_content
    return body


def ids(body: dict[str, Any]) -> list[int]:
    return [row["submission"] for row in body["submissions"]]


# --- list_submissions: filters ---------------------------------------------------------------


def test_needs_review_is_the_default_and_is_the_review_queue(
    services: McpServices, settings: Settings, seeded: Seeded
) -> None:
    assert ids(listed(services, settings)) == [seeded.waiting]


def test_failed_goes_by_the_effective_verdict(
    services: McpServices, settings: Settings, seeded: Seeded
) -> None:
    assert ids(listed(services, settings, filter="failed")) == [seeded.far]


def test_passed_goes_by_the_effective_verdict(
    services: McpServices, settings: Settings, seeded: Seeded
) -> None:
    body = listed(services, settings, filter="passed")

    assert ids(body) == [seeded.overturned, seeded.changed, seeded.approved, seeded.passed]


def test_overridden_is_every_photo_the_moderator_ruled_on(
    services: McpServices, settings: Settings, seeded: Seeded
) -> None:
    body = listed(services, settings, filter="overridden")

    assert ids(body) == [seeded.overturned, seeded.changed, seeded.approved]


def test_all_is_everything_in_the_session_newest_first(
    services: McpServices, settings: Settings, seeded: Seeded
) -> None:
    body = listed(services, settings, filter="all")

    assert ids(body) == sorted(ids(body), reverse=True)
    assert seeded.elsewhere not in ids(body)
    assert len(ids(body)) == 6


def test_a_row_has_both_verdicts_the_ruling_and_the_failed_checks(
    services: McpServices, settings: Settings, seeded: Seeded
) -> None:
    rows = {
        row["submission"]: row for row in listed(services, settings, filter="all")["submissions"]
    }

    assert rows[seeded.far] == {
        "submission": seeded.far,
        "team": "Foxes",
        "checkpoint": {"sequence": 1, "name": "Lion fountain"},
        "attempt": 2,
        "received-at": "2026-10-03T10:02:00Z",
        "verdict": "failed",
        "ruling": None,
        "effective-verdict": "failed",
        "failed-checks": ["in_range", "in_window"],
    }
    assert (rows[seeded.overturned]["verdict"], rows[seeded.overturned]["ruling"]) == (
        "failed",
        "approve",
    )
    assert rows[seeded.overturned]["effective-verdict"] == "pass"
    assert rows[seeded.changed]["ruling"] == "approve"


def test_team_filters_ignoring_case(
    services: McpServices, settings: Settings, seeded: Seeded
) -> None:
    body = listed(services, settings, filter="all", team="hERons")

    assert ids(body) == [seeded.approved, seeded.waiting]
    assert ids(listed(services, settings, filter="all", team="Nobody")) == []


def test_checkpoint_filters(services: McpServices, settings: Settings, seeded: Seeded) -> None:
    body = listed(services, settings, filter="all", checkpoint=2)

    assert ids(body) == [seeded.changed, seeded.waiting]
    assert {row["checkpoint"]["name"] for row in body["submissions"]} == {"Bandstand"}


def test_the_filters_combine(services: McpServices, settings: Settings, seeded: Seeded) -> None:
    body = listed(services, settings, filter="passed", team="Foxes", checkpoint=1)

    assert ids(body) == [seeded.overturned, seeded.passed]


# --- list_submissions: paging ------------------------------------------------------------------


def test_paging_walks_the_whole_list(
    services: McpServices, settings: Settings, seeded: Seeded
) -> None:
    everything = ids(listed(services, settings, filter="all"))

    first = listed(services, settings, filter="all", limit=4)
    second = listed(services, settings, filter="all", limit=4, before=first["next"])

    assert ids(first) == everything[:4]
    assert first["next"] == everything[3]
    assert ids(second) == everything[4:]
    assert second["next"] is None


def test_a_page_that_ends_the_list_has_no_next(
    services: McpServices, settings: Settings, seeded: Seeded
) -> None:
    body = listed(services, settings, filter="all", limit=6)

    assert body["next"] is None


@pytest.mark.parametrize("bad", [{"limit": 0}, {"limit": 101}, {"before": 0}, {"checkpoint": 0}])
def test_arguments_out_of_range_are_refused(
    services: McpServices, settings: Settings, seeded: Seeded, bad: dict[str, int]
) -> None:
    result = call_tool(services, settings, "list_submissions", {"session_id": SESSION, **bad})

    assert result.is_error


def test_the_link_to_the_moderator_screen(
    services: McpServices, settings: Settings, seeded: Seeded
) -> None:
    assert listed(services, settings)["moderator-link"] == f"{WEB}moderator?session={SESSION}"
    assert listed(services, Settings())["moderator-link"] is None


def test_an_unknown_session(services: McpServices, settings: Settings, seeded: Seeded) -> None:
    result = call_tool(
        services,
        settings,
        "list_submissions",
        {"session_id": "00000000-0000-4000-8000-000000000000"},
    )

    assert error_of(result) == {"code": "unknown_session", "detail": "unknown session"}


# --- get_submission ----------------------------------------------------------------------------


def read(services: McpServices, settings: Settings, submission: int) -> dict[str, Any]:
    result = call_tool(
        services, settings, "get_submission", {"session_id": SESSION, "submission_id": submission}
    )
    assert not result.is_error, result.content
    body: dict[str, Any] = result.structured_content
    return body


def test_a_submission_in_full(services: McpServices, settings: Settings, seeded: Seeded) -> None:
    body = read(services, settings, seeded.overturned)

    assert body["submission"] == seeded.overturned
    assert body["team"] == "Foxes"
    assert body["checkpoint"] == {"sequence": 1, "name": "Lion fountain"}
    assert (body["pose"], body["scene"]) == (POSE, SCENE)
    assert (body["verdict"], body["effective-verdict"]) == ("failed", "pass")
    assert body["checks"] == [
        {
            "check": "pose_correct",
            "outcome": "uncertain",
            "confidence": 0.62,
            "reason": POSE_UNSURE.reason,
            "detail": "One arm raised, not both.",
        }
    ]
    assert body["referee"] == {"status": "ok", "error-code": None}
    assert [(r["ruling"], r["note"]) for r in body["rulings"]] == [("approve", "The pose was fine")]
    assert body["moderator-link"] == f"{WEB}moderator?session={SESSION}"


def test_every_ruling_is_returned_oldest_first(
    services: McpServices, settings: Settings, seeded: Seeded
) -> None:
    body = read(services, settings, seeded.changed)

    assert [(r["ruling"], r["note"]) for r in body["rulings"]] == [
        ("reject", "Not the pose"),
        ("approve", None),
    ]
    assert body["referee"] == {"status": "error", "error-code": "deadline"}


def test_a_submission_without_a_referee_call_or_rulings(
    services: McpServices, settings: Settings, seeded: Seeded
) -> None:
    body = read(services, settings, seeded.passed)

    assert (body["referee"], body["rulings"], body["pose"]) == (None, [], None)


def test_a_checkpoint_without_a_challenge_has_no_scene(
    services: McpServices, settings: Settings, seeded: Seeded
) -> None:
    assert read(services, settings, seeded.waiting)["scene"] is None


def test_the_trace_the_prompt_and_the_tokens_are_not_returned(
    services: McpServices, settings: Settings, seeded: Seeded
) -> None:
    text = json.dumps(read(services, settings, seeded.waiting))

    for hidden in ("prompt", "token", "user-text", "judgement", "sha256", "cost"):
        assert hidden not in text


def test_an_unknown_submission(services: McpServices, settings: Settings, seeded: Seeded) -> None:
    result = call_tool(
        services, settings, "get_submission", {"session_id": SESSION, "submission_id": 99999}
    )

    assert error_of(result) == {"code": "unknown_submission", "detail": "unknown submission"}


def test_another_sessions_submission_is_unknown(
    services: McpServices, settings: Settings, seeded: Seeded
) -> None:
    result = call_tool(
        services,
        settings,
        "get_submission",
        {"session_id": SESSION, "submission_id": seeded.elsewhere},
    )

    assert error_of(result)["code"] == "unknown_submission"


def test_get_submission_in_an_unknown_session(
    services: McpServices, settings: Settings, seeded: Seeded
) -> None:
    result = call_tool(
        services,
        settings,
        "get_submission",
        {"session_id": "00000000-0000-4000-8000-000000000000", "submission_id": seeded.passed},
    )

    assert error_of(result)["code"] == "unknown_session"


# --- privacy -------------------------------------------------------------------------------------


def test_no_result_holds_a_photo_a_participant_a_coordinate_or_a_code(
    services: McpServices, settings: Settings, seeded: Seeded, db: Any
) -> None:
    images = [row["image_id"] for row in db.execute("SELECT image_id FROM submissions").fetchall()]
    results = [
        call_tool(services, settings, "list_submissions", {"session_id": SESSION, "filter": f})
        for f in ("needs_review", "failed", "passed", "overridden", "all")
    ]
    results += [
        call_tool(services, settings, "get_submission", {"session_id": SESSION, "submission_id": n})
        for n in (seeded.passed, seeded.far, seeded.waiting, seeded.changed, seeded.overturned)
    ]

    for result in results:
        assert not result.is_error
        assert all(isinstance(block, TextContent) for block in result.content)
    everything = json.dumps([[b.text for b in r.content] + [r.structured_content] for r in results])  # type: ignore[union-attr]
    secrets = [
        str(seeded.fox),
        str(seeded.heron),
        *map(str, images),
        str(LAT),
        str(LONG),
        FOX,
        HERON,
        MODERATOR,
        "image/jpeg",
    ]
    for secret in secrets:
        assert secret not in everything
    assert len(images) == 7

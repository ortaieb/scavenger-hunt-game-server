"""The moderator's referee traces: `GET /sessions/{session}/traces`."""

import json
import random
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import anthropic
import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from images import jpeg, scene
from pytest_mock import MockerFixture

from game_server.app import create_app
from game_server.checks import CheckResult
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.models import VerdictStatus
from game_server.referee import (
    ClaudeReferee,
    ModelReply,
    RefereeCall,
    RefereeJudgement,
    RefereeReport,
    SentImage,
    VisualCheckJudgement,
    get_referee,
    prompt_sha256,
    system_prompt,
)
from game_server.sessions import SessionRepository, get_session_repository
from game_server.submissions import NewSubmission, SubmissionStore

SESSION = "aeffe667-4f9f-4108-b5e2-56ae821fe413"
OTHER = "0b5e9c1e-2f7a-4d8e-9a57-3c1f6f0d2b44"
MODERATOR = "MOD-8H3T-QX"
OTHER_MODERATOR = "MOD-2KV9-ZP"
FOX, HERON = "FOX-7Q2K", "HERON-4MXP"
START = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
DURING = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
SCENE = "A stone fountain"
LAT, LONG = 51.5034, -0.1276
PHOTOS = count(500)  # distinct seeds: no photo is a duplicate of another
MODEL_OUTPUT = {
    "scene_matches": {
        "reason": "A fountain behind one person.",
        "verdict": "pass",
        "confidence": 0.9,
    },
    "pose_correct": {"reason": "One arm raised, not both.", "verdict": "unsure", "confidence": 0.6},
}

PROMPT = "You are the referee."
REVISED = "You are the referee. Judge the pose too."
RULING = VisualCheckJudgement(reason="A fountain.", verdict="pass", confidence=0.9)
JUDGEMENT = RefereeJudgement(scene_matches=RULING, pose_correct=RULING)
SENT = SentImage(sha256="ab" * 32, width=1568, height=1045)
CALL = RefereeCall(
    PROMPT,
    "<scene>A fountain</scene>",
    SENT,
    stop_reason="end_turn",
    response_text='{"scene_matches": …}',
)
OK = RefereeReport(
    status="ok",
    judgement=JUDGEMENT,
    model="claude-haiku-4-5-20251001",
    request_id="req_123",
    input_tokens=1500,
    output_tokens=120,
    latency_ms=950,
    cost_usd=Decimal("0.0021"),
    call=CALL,
)
TIMEOUT = RefereeReport(
    status="error",
    error_code="timeout",
    model="claude-haiku-4-5",
    latency_ms=20000,
    call=RefereeCall(PROMPT, "<scene>A fountain</scene>", SENT),
)
CHECKED_IN = CheckResult("checked_in", "passed", 1.0, "You checked in.", detail="arrival 7")
POSE_UNSURE = CheckResult(
    "pose_correct", "uncertain", 0.6, "A moderator will review it.", detail="Arms half up."
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
                "challenge": {"scene": f"{SCENE} {n}", "pose": "Wave"},
            }
            for n in (1, 2)
        ],
        "teams": [
            {"name": "Red Foxes", "join-code": f"{FOX}{suffix}", "order": [1, 2]},
            {"name": "Blue Herons", "join-code": f"{HERON}{suffix}", "order": [2, 1]},
        ],
    }


@pytest.fixture
def client(
    tmp_path: Path, mocker: MockerFixture, load_sessions: Callable[[str], SessionRepository]
) -> Iterator[TestClient]:
    app = create_app()
    # The real referee, with only the SDK call faked.
    reply = ModelReply(
        "end_turn", json.dumps(MODEL_OUTPUT), "claude-haiku-4-5", "req_traces", 1500, 120
    )
    mocker.patch("game_server.referee._create_structured_message", return_value=reply)
    sdk = anthropic.Anthropic(api_key="test-key-not-used")
    referee = ClaudeReferee(sdk, "claude-haiku-4-5", max_image_edge=1568)
    app.dependency_overrides[get_referee] = lambda: referee
    settings = Settings(image_base_path=tmp_path / "images")
    app.dependency_overrides[get_settings] = lambda: settings
    sessions = load_sessions(
        json.dumps(
            [session_json(SESSION, MODERATOR), session_json(OTHER, OTHER_MODERATOR, "-OTHER")]
        )
    )
    app.dependency_overrides[get_session_repository] = lambda: sessions
    app.dependency_overrides[get_clock] = lambda: lambda: DURING
    with TestClient(app) as test_client:
        yield test_client


def get_traces(client: TestClient, session: str = SESSION, **params: Any) -> Response:
    code = MODERATOR if session == SESSION else OTHER_MODERATOR
    return client.get(
        f"/sessions/{session}/traces", params=params, headers={"Authorization": f"Bearer {code}"}
    )


def traces(client: TestClient, **params: Any) -> dict[str, Any]:
    response = get_traces(client, **params)
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def walk(client: TestClient, limit: int) -> list[dict[str, Any]]:
    """Every page, following `next` until it's null."""
    pages = [traces(client, limit=limit)]
    while pages[-1]["next"] is not None:
        pages.append(traces(client, limit=limit, before=pages[-1]["next"]))
    return pages


def join(client: TestClient, code: str = FOX) -> str:
    participant: str = client.post("/join", json={"code": code, "consent": True}).json()[
        "participant"
    ]
    return participant


def photograph(client: TestClient, participant: str) -> Response:
    metadata = {
        "session": SESSION,
        "participant": participant,
        "checkpoint": 1,
        "location": {"lat": LAT, "long": LONG},
        "capture-time": DURING.isoformat(),
    }
    return client.post(
        "/challenge",
        files={
            "metadata": (None, json.dumps(metadata), "application/json"),
            "challenge-image": ("p.jpeg", jpeg(scene(next(PHOTOS))), "image/jpeg"),
        },
    )


def record(
    store: SubmissionStore,
    *,
    session: str = SESSION,
    participant: UUID | None = None,
    checkpoint: int = 1,
    verdict: VerdictStatus = "pending",
    processing_ms: int = 40,
    checks: tuple[CheckResult, ...] = (),
    referee: RefereeReport | None = None,
    image_id: UUID | None = None,
) -> int:
    """Record a submission directly; returns its id."""
    return store.record(
        NewSubmission(
            session=UUID(session),
            participant=participant or uuid4(),
            checkpoint=checkpoint,
            received_at=DURING,
            capture_time=DURING,
            lat=LAT,
            long=LONG,
            image_id=image_id or uuid4(),
            verdict=verdict,
            checks=checks,
            distance_m=1.0,
            phash=0,
            processing_ms=processing_ms,
            referee=referee,
        )
    ).id


# --- the response ----------------------------------------------------------------------------


def test_an_empty_session(client: TestClient, store: SubmissionStore) -> None:
    record(store, session=OTHER, referee=OK)  # another session's data stays out

    assert traces(client) == {
        "summary": {
            "submissions": 0,
            "verdicts": {"pass": 0, "pending": 0, "failed": 0},
            "rulings": {"approve": 0, "reject": 0},
            "referee-calls": 0,
            "referee-errors": 0,
            "cost-usd": "0",
            "processing-ms": {"p50": None, "p95": None, "max": None},
        },
        "prompts": {},
        "items": [],
        "next": None,
    }


def test_a_traced_submission(client: TestClient, store: SubmissionStore) -> None:
    fox, image_id = UUID(join(client)), uuid4()
    submission = record(
        store,
        participant=fox,
        checkpoint=2,
        processing_ms=3120,
        checks=(CHECKED_IN, POSE_UNSURE),
        referee=OK,
        image_id=image_id,
    )

    assert traces(client) == {
        "summary": {
            "submissions": 1,
            "verdicts": {"pass": 0, "pending": 1, "failed": 0},
            "rulings": {"approve": 0, "reject": 0},
            "referee-calls": 1,
            "referee-errors": 0,
            "cost-usd": "0.0021",
            "processing-ms": {"p50": 3120, "p95": 3120, "max": 3120},
        },
        "prompts": {prompt_sha256(PROMPT): PROMPT},
        "items": [
            {
                "submission": submission,
                "team": "Red Foxes",
                "checkpoint": 2,
                "attempt": 1,
                "received-at": "2026-10-03T10:00:00Z",
                "verdict": "pending",
                "processing-ms": 3120,
                "image-id": str(image_id),
                "checks": [
                    {
                        "check": "checked_in",
                        "outcome": "passed",
                        "confidence": 1.0,
                        "reason": "You checked in.",
                        "detail": "arrival 7",
                    },
                    {
                        "check": "pose_correct",
                        "outcome": "uncertain",
                        "confidence": 0.6,
                        "reason": "A moderator will review it.",
                        "detail": "Arms half up.",
                    },
                ],
                "trace": {
                    "model": "claude-haiku-4-5-20251001",
                    "status": "ok",
                    "error-code": None,
                    "stop-reason": "end_turn",
                    "request-id": "req_123",
                    "prompt-sha256": prompt_sha256(PROMPT),
                    "user-text": "<scene>A fountain</scene>",
                    "references": [],
                    "judgement": JUDGEMENT.model_dump(mode="json"),
                    "input-tokens": 1500,
                    "output-tokens": 120,
                    "cost-usd": "0.0021",
                    "latency-ms": 950,
                },
                "ruling": None,
            }
        ],
        "next": None,
    }


def test_a_photo_judged_by_the_referee_shows_its_call(client: TestClient) -> None:
    fox = join(client)
    client.post(f"/sessions/{SESSION}/start", headers={"Authorization": f"Bearer {MODERATOR}"})
    client.post(f"/sessions/{SESSION}/participants/{fox}/arrive", json={"checkpoint": 1})
    photo = photograph(client, fox).json()

    body = traces(client)

    [item] = body["items"]
    assert (item["team"], item["verdict"], item["image-id"]) == (
        "Red Foxes",
        "pending",
        photo["image_id"],
    )
    trace = item["trace"]
    assert (trace["status"], trace["request-id"], trace["cost-usd"]) == (
        "ok",
        "req_traces",
        "0.0021",
    )
    assert f"<scene>\n{SCENE} 1\n</scene>" in trace["user-text"]
    assert "<pose>\nWave\n</pose>" in trace["user-text"]
    assert trace["judgement"] == MODEL_OUTPUT
    assert body["prompts"] == {prompt_sha256(system_prompt()): system_prompt()}
    assert trace["prompt-sha256"] == prompt_sha256(system_prompt())
    # The model's reasons beside what the player was told.
    details = {check["check"]: check["detail"] for check in item["checks"]}
    assert details["scene_matches"] == MODEL_OUTPUT["scene_matches"]["reason"]
    assert details["pose_correct"] == MODEL_OUTPUT["pose_correct"]["reason"]


def test_a_photo_the_referee_was_not_consulted_on_has_no_trace(client: TestClient) -> None:
    fox = join(client)
    client.post(f"/sessions/{SESSION}/start", headers={"Authorization": f"Bearer {MODERATOR}"})
    photograph(client, fox)  # without checking in

    body = traces(client)

    [item] = body["items"]
    assert (item["verdict"], item["trace"]) == ("failed", None)
    details = {check["check"]: check["detail"] for check in item["checks"]}
    assert details["checked_in"] == "no arrival at this checkpoint"
    assert details["scene_matches"] == "referee not consulted: an earlier check failed"
    assert (body["summary"]["referee-calls"], body["prompts"]) == (0, {})


def test_a_disabled_referee_leaves_no_trace(client: TestClient, store: SubmissionStore) -> None:
    record(store, referee=RefereeReport(status="disabled"))

    body = traces(client)

    assert [item["trace"] for item in body["items"]] == [None]
    assert body["summary"]["referee-calls"] == 0


def test_an_errored_call_has_its_error_code(client: TestClient, store: SubmissionStore) -> None:
    record(store, referee=TIMEOUT)

    body = traces(client)

    trace = body["items"][0]["trace"]
    assert (trace["status"], trace["error-code"], trace["latency-ms"]) == (
        "error",
        "timeout",
        20000,
    )
    for field in ("stop-reason", "request-id", "judgement", "input-tokens", "cost-usd"):
        assert trace[field] is None, field
    assert body["prompts"] == {prompt_sha256(PROMPT): PROMPT}
    assert (body["summary"]["referee-calls"], body["summary"]["referee-errors"]) == (1, 1)


def test_a_ruled_submission_shows_its_latest_ruling_beside_the_referees_verdict(
    client: TestClient, store: SubmissionStore
) -> None:
    ruled = record(store, verdict="pending", referee=OK)
    record(store, verdict="pending")
    store.rule(UUID(SESSION), ruled, "approve", "Looks right", DURING)
    store.rule(UUID(SESSION), ruled, "reject", "Wrong fountain", DURING + timedelta(minutes=4))

    unruled, item = traces(client)["items"]

    assert item["submission"] == ruled
    assert item["verdict"] == "pending"  # the referee's, unchanged
    assert item["ruling"] == {
        "ruling": "reject",
        "note": "Wrong fountain",
        "ruled-at": "2026-10-03T10:04:00Z",
    }
    assert item["trace"]["status"] == "ok"
    assert unruled["ruling"] is None


def test_a_submission_without_a_participant_row_is_still_listed(
    client: TestClient, store: SubmissionStore
) -> None:
    record(store)  # a participant that never joined

    assert [item["team"] for item in traces(client)["items"]] == [None]


# --- the summary -----------------------------------------------------------------------------


def test_the_summary_counts_verdicts_calls_and_errors(
    client: TestClient, store: SubmissionStore
) -> None:
    refused = replace(OK, status="error", error_code="refusal", judgement=None)
    submissions: list[tuple[VerdictStatus, RefereeReport | None]] = [
        ("pass", OK),
        ("pass", OK),
        ("pending", TIMEOUT),
        ("failed", refused),
        ("failed", None),
        ("failed", None),
    ]
    for verdict, referee in submissions:
        record(store, verdict=verdict, referee=referee)
    record(store, session=OTHER, verdict="pass", referee=TIMEOUT)

    summary = traces(client)["summary"]

    assert summary["submissions"] == 6
    assert summary["verdicts"] == {"pass": 2, "pending": 1, "failed": 3}
    assert (summary["referee-calls"], summary["referee-errors"]) == (4, 2)
    assert summary["cost-usd"] == "0.0063"  # a refusal is billed; a timeout isn't


def test_the_summary_counts_latest_rulings_and_keeps_the_referees_verdicts(
    client: TestClient, store: SubmissionStore
) -> None:
    approved, rejected, changed = (record(store, verdict="pending") for _ in range(3))
    record(store, verdict="failed")
    elsewhere = record(store, session=OTHER, verdict="pending")
    store.rule(UUID(SESSION), approved, "approve", None, DURING)
    store.rule(UUID(SESSION), rejected, "reject", None, DURING)
    store.rule(UUID(SESSION), changed, "reject", None, DURING)
    store.rule(UUID(SESSION), changed, "approve", None, DURING)
    store.rule(UUID(OTHER), elsewhere, "reject", None, DURING)

    summary = traces(client)["summary"]

    assert summary["rulings"] == {"approve": 2, "reject": 1}
    assert summary["verdicts"] == {"pass": 0, "pending": 3, "failed": 1}


def test_costs_are_exact_decimal_strings(client: TestClient, store: SubmissionStore) -> None:
    for cost in ("0.0000001", "0.0021", None):  # None: a model without a price
        record(store, referee=replace(OK, cost_usd=Decimal(cost) if cost else None))

    body = traces(client)

    assert [item["trace"]["cost-usd"] for item in body["items"]] == [None, "0.0021", "0.0000001"]
    assert body["summary"]["cost-usd"] == "0.0021001"


def test_processing_times_are_nearest_rank_over_the_whole_session(
    client: TestClient, store: SubmissionStore
) -> None:
    times = [100 * n for n in range(1, 22)]  # 21 submissions: p50 is the 11th, p95 the 20th
    random.Random(7).shuffle(times)
    for processing_ms in times:
        record(store, processing_ms=processing_ms)
    record(store, session=OTHER, processing_ms=99_999)

    pages = walk(client, limit=5)

    assert len(pages) == 5
    for page in pages:
        assert page["summary"]["processing-ms"] == {"p50": 1100, "p95": 2000, "max": 2100}
        assert page["summary"]["submissions"] == 21


# --- paging ----------------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [1, 3, 7, 10])
def test_paging_walks_every_submission_once_newest_first(
    client: TestClient, store: SubmissionStore, limit: int
) -> None:
    ids = []
    for n in range(7):
        ids.append(record(store, referee=OK if n % 2 else None))
        record(store, session=OTHER, referee=OK)  # interleaved ids from another session

    pages = walk(client, limit=limit)

    walked = [item["submission"] for page in pages for item in page["items"]]
    assert walked == sorted(ids, reverse=True)
    assert len(pages) == -(-len(ids) // limit)  # a full last page has no next
    assert all(len(page["items"]) <= limit for page in pages)
    assert all(page["summary"] == pages[0]["summary"] for page in pages)
    assert pages[0]["summary"]["submissions"] == len(ids)


def test_next_is_the_last_submission_on_the_page(
    client: TestClient, store: SubmissionStore
) -> None:
    ids = [record(store) for _ in range(3)]

    first = traces(client, limit=2)

    assert [item["submission"] for item in first["items"]] == [ids[2], ids[1]]
    assert first["next"] == ids[1]


def test_the_default_page_is_fifty(client: TestClient, store: SubmissionStore) -> None:
    for _ in range(51):
        record(store)

    first = traces(client)

    assert len(first["items"]) == 50
    assert len(traces(client, before=first["next"])["items"]) == 1
    assert len(traces(client, limit=100)["items"]) == 51


def test_a_page_below_the_oldest_submission_is_empty(
    client: TestClient, store: SubmissionStore
) -> None:
    oldest = record(store)
    record(store)

    body = traces(client, before=oldest)

    assert (body["items"], body["prompts"], body["next"]) == ([], {}, None)
    assert body["summary"]["submissions"] == 2


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": "ten"},
        {"before": 0},
        {"before": 2**63},  # past BIGINT
        {"before": "x"},
    ],
    ids=["limit-0", "limit-101", "limit-text", "before-0", "before-too-big", "before-text"],
)
def test_invalid_paging_is_422(client: TestClient, params: dict[str, Any]) -> None:
    assert get_traces(client, **params).status_code == 422


# --- prompts ---------------------------------------------------------------------------------


def test_prompts_hold_each_hash_on_the_page_once(
    client: TestClient, store: SubmissionStore
) -> None:
    revised = replace(OK, call=replace(CALL, system_prompt=REVISED))
    record(store, referee=revised)
    record(store, referee=OK)
    record(store, referee=OK)
    record(store)

    first, second = walk(client, limit=3)

    assert first["prompts"] == {prompt_sha256(PROMPT): PROMPT}
    assert second["prompts"] == {prompt_sha256(REVISED): REVISED}
    for page in (first, second):
        hashes = {i["trace"]["prompt-sha256"] for i in page["items"] if i["trace"] is not None}
        assert set(page["prompts"]) == hashes


# --- authorisation and secrecy -------------------------------------------------------------


@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer MOD-WRONG-1", f"Bearer {OTHER_MODERATOR}", f"Bearer {FOX}"],
    ids=["no-code", "wrong", "other-session", "join-code"],
)
def test_moderator_code_required(
    client: TestClient, store: SubmissionStore, authorization: str | None
) -> None:
    record(store, referee=OK)
    headers = {"Authorization": authorization} if authorization else {}

    response = client.get(f"/sessions/{SESSION}/traces", headers=headers)

    assert response.status_code == 401
    assert response.json() == {
        "detail": "moderator code required",
        "code": "moderator_unauthorised",
    }
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_unknown_session_is_404(client: TestClient) -> None:
    response = client.get(
        f"/sessions/{uuid4()}/traces", headers={"Authorization": f"Bearer {MODERATOR}"}
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "unknown session"}


def test_the_traces_reveal_no_coordinates_codes_or_participants(client: TestClient) -> None:
    fox = join(client)
    client.post(f"/sessions/{SESSION}/start", headers={"Authorization": f"Bearer {MODERATOR}"})
    arrival = client.post(f"/sessions/{SESSION}/participants/{fox}/arrive", json={"checkpoint": 1})
    photograph(client, fox)
    photograph(client, fox)  # the check-in is used up: no trace

    text = get_traces(client).text

    assert SCENE in text  # the scene is for the moderator
    for leak in (str(LAT), str(LONG), "distance", "proximity", "Clue 1", FOX, HERON, MODERATOR):
        assert leak not in text
    assert fox not in text
    assert f'"{arrival.json()["code"]}"' not in text  # the one-time code

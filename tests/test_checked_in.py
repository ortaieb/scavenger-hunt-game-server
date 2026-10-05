"""POST /challenge held to the team's check-in (#61): the `checked_in` check, and the referee
judging the pose issued at arrival. Fixed clock, fake referee."""

import json
import logging
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from images import jpeg, scene
from psycopg.rows import DictRow
from pytest_mock import MockerFixture

from game_server import arrive as arrive_module
from game_server.app import create_app
from game_server.arrivals import Arrival, LatestArrival
from game_server.challenge import judge_and_record
from game_server.checks import SubmissionContext, run_checks
from game_server.checks.checked_in import CHECK_IN_EXPIRED, NOT_CHECKED_IN, CheckedInCheck
from game_server.clock import Stopwatch, get_clock
from game_server.config import Settings, get_settings
from game_server.models import ChallengeMetadata, Location
from game_server.phash import perceptual_hash
from game_server.referee import (
    RefereeJudgement,
    RefereeReport,
    VisualCheckJudgement,
    get_referee,
)
from game_server.sessions import (
    Checkpoint,
    GameSession,
    SessionRepository,
    VisualChallenge,
    get_session_repository,
    parse_sessions,
)
from game_server.storage import ImageStore
from game_server.submissions import SubmissionStore

SESSION = "aeffe667-4f9f-4108-b5e2-56ae821fe413"
START = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 3, 9, 30, tzinfo=UTC)
TTL = timedelta(minutes=10)
FOX = "FOX-7Q2K"
SCENE = "A granite fountain"
POSE = "Arms raised"
NEW_POSE = "Hands on hips"
PHOTOS = count(500)  # distinct seeds: no photo is a duplicate of another

Db = psycopg.Connection[DictRow]


def repository(challenges: dict[int, dict[str, str] | None]) -> SessionRepository:
    """Checkpoints 1 and 2 at the same place, on the Red Foxes' route in that order."""
    checkpoints = [
        {
            "sequence": sequence,
            "name": f"Name {sequence}",
            "clue": f"Clue {sequence}",
            "location": {"lat": 51.5, "long": -0.1},
            "proximity": 40,
            **({"challenge": challenge} if challenge else {}),
        }
        for sequence, challenge in challenges.items()
    ]
    return parse_sessions(
        json.dumps(
            [
                {
                    "id": SESSION,
                    "name": "Hunt",
                    "location": "Here",
                    "start-time": START.isoformat(),
                    "end-time": "2026-10-03T12:00:00Z",
                    "checkpoints": checkpoints,
                    "teams": [{"name": "Red Foxes", "join-code": FOX, "order": [1, 2]}],
                }
            ]
        )
    )


CHALLENGES: dict[int, dict[str, str] | None] = {
    1: {"scene": SCENE, "pose": POSE},
    2: {"scene": "A red door", "pose": "Wave"},
}


def passing() -> RefereeReport:
    check = VisualCheckJudgement(reason="fine", verdict="pass", confidence=0.95)
    return RefereeReport(
        status="ok",
        judgement=RefereeJudgement(scene_matches=check, pose_correct=check),
        model="fake",
    )


@dataclass
class FakeReferee:
    calls: list[VisualChallenge] = field(default_factory=list)

    def judge(self, image: bytes, challenge: VisualChallenge) -> RefereeReport:
        self.calls.append(challenge)
        return passing()


@dataclass
class Game:
    """The app under test, with its clock and sessions file under the test's control."""

    client: TestClient
    referee: FakeReferee
    clock: list[datetime]
    sessions: list[SessionRepository]

    def join(self) -> str:
        response = self.client.post("/join", json={"code": FOX, "consent": True})
        participant: str = response.json()["participant"]
        return participant

    def arrive(self, participant: str, checkpoint: int = 1) -> Response:
        return self.client.post(
            f"/sessions/{SESSION}/participants/{participant}/arrive",
            json={"checkpoint": checkpoint},
        )

    def photograph(self, participant: str, checkpoint: int = 1, **changes: Any) -> Response:
        metadata = {
            "session": SESSION,
            "participant": participant,
            "checkpoint": checkpoint,
            "location": {"lat": 51.5001, "long": -0.1},
            "capture-time": self.clock[0].isoformat(),
            **changes,
        }
        return self.client.post(
            "/challenge",
            files={
                "metadata": (None, json.dumps(metadata), "application/json"),
                "challenge-image": ("p.jpeg", jpeg(scene(next(PHOTOS))), "image/jpeg"),
            },
        )


@pytest.fixture
def game(tmp_path: Path, store: SubmissionStore) -> Iterator[Game]:
    store.start_run(UUID(SESSION), START)
    app = create_app()
    game_ = Game(TestClient(app), FakeReferee(), clock=[NOW], sessions=[repository(CHALLENGES)])
    settings = Settings(
        image_base_path=tmp_path / "images", arrival_code_ttl_seconds=int(TTL.total_seconds())
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: game_.sessions[0]
    app.dependency_overrides[get_clock] = lambda: lambda: game_.clock[0]
    app.dependency_overrides[get_referee] = lambda: game_.referee
    with game_.client:
        yield game_


def checkpoint_verdict(response: Response) -> dict[str, Any]:
    result: dict[str, Any] = response.json()["verdict"]["checkpoint"]
    return result


def check(response: Response, name: str) -> dict[str, Any]:
    results: list[dict[str, Any]] = checkpoint_verdict(response)["checks"]
    [result] = [c for c in results if c["check"] == name]
    return result


def codes(response: Response) -> list[str]:
    return [r["code"] for r in checkpoint_verdict(response)["rejections"]]


def rows(db: Db) -> list[DictRow]:
    return db.execute("SELECT * FROM submissions ORDER BY id").fetchall()


# --- end to end -----------------------------------------------------------------------


def test_join_arrive_photo_passes_and_the_referee_judges_the_issued_pose(game: Game) -> None:
    participant = game.join()
    assert game.arrive(participant).json()["pose"] == POSE
    # The sessions file changes between arriving and sending: the player was shown POSE.
    game.sessions[0] = repository({**CHALLENGES, 1: {"scene": SCENE, "pose": NEW_POSE}})

    response = game.photograph(participant)

    assert response.status_code == 200
    assert checkpoint_verdict(response)["verdict"] == "pass"
    assert check(response, "checked_in") == {
        "check": "checked_in",
        "outcome": "passed",
        "confidence": 1.0,
        "reason": "You checked in at this checkpoint.",
    }
    assert game.referee.calls == [VisualChallenge(scene=SCENE, pose=POSE)]


def test_without_arriving_the_photo_fails_and_the_referee_is_not_called(game: Game) -> None:
    participant = game.join()

    response = game.photograph(participant)

    assert response.status_code == 200
    assert checkpoint_verdict(response)["verdict"] == "failed"
    assert checkpoint_verdict(response)["rejections"] == [
        {
            "code": "not_checked_in",
            "message": "Tap I'm here at the checkpoint before sending a photo.",
        }
    ]
    assert game.referee.calls == []
    assert check(response, "scene_matches")["outcome"] == "skipped"
    assert check(response, "pose_correct")["outcome"] == "skipped"


def test_an_expired_check_in_fails(game: Game) -> None:
    participant = game.join()
    game.arrive(participant)
    game.clock[0] += TTL  # expiry is exclusive

    response = game.photograph(participant)

    assert checkpoint_verdict(response)["rejections"] == [
        {
            "code": "check_in_expired",
            "message": "Your check-in ran out. Tap I'm here again, then send your photo.",
        }
    ]
    assert game.referee.calls == []


def test_a_used_check_in_cant_be_used_again_until_a_fresh_arrive(game: Game) -> None:
    participant = game.join()
    game.arrive(participant)
    far = game.photograph(participant, location={"lat": 51.51, "long": -0.1})

    again = game.photograph(participant)
    fresh = game.arrive(participant)  # the same instant: the clock is fixed
    after = game.photograph(participant)

    assert (codes(far), check(far, "checked_in")["outcome"]) == (["out_of_range"], "passed")
    assert codes(again) == ["not_checked_in"]
    assert fresh.status_code == 201
    assert checkpoint_verdict(after)["verdict"] == "pass"


def test_a_photo_for_another_checkpoint_is_not_checked_in(game: Game) -> None:
    participant = game.join()
    game.arrive(participant)  # at checkpoint 1, its current one

    response = game.photograph(participant, checkpoint=2)

    assert codes(response) == ["not_checked_in"]
    assert game.referee.calls == []


def test_without_a_pose_at_check_in_the_visual_checks_skip(game: Game) -> None:
    game.sessions[0] = repository({**CHALLENGES, 1: None})
    participant = game.join()
    assert game.arrive(participant).json()["pose"] is None
    game.sessions[0] = repository(CHALLENGES)  # the challenge is added afterwards

    response = game.photograph(participant)

    assert response.status_code == 202
    assert checkpoint_verdict(response)["verdict"] == "pending"
    assert check(response, "checked_in")["outcome"] == "passed"
    assert check(response, "scene_matches")["outcome"] == "skipped"
    assert check(response, "pose_correct")["outcome"] == "skipped"
    assert game.referee.calls == []


def test_the_arrival_is_recorded_but_its_code_never_logged_or_returned(
    game: Game, db: Db, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    mocker.patch.object(arrive_module, "draw_code", return_value="SENTINEL-OTC")
    caplog.set_level(logging.DEBUG)
    participant = game.join()
    game.arrive(participant)
    [arrival] = db.execute("SELECT id FROM arrivals").fetchall()

    passed = game.photograph(participant)
    unchecked = game.photograph(participant)

    first, second = rows(db)
    assert (first["arrival_id"], second["arrival_id"]) == (arrival["id"], None)
    assert f"checkpoint 1 attempt 1 arrival {arrival['id']} distance" in caplog.text
    assert "checkpoint 1 attempt 2 arrival none distance" in caplog.text
    checks = (*first["checks"], *second["checks"])
    details = [c["detail"] for c in checks if c["check"] == "checked_in"]
    used = arrival["id"]
    assert details == [f"arrival {used}", f"arrival {used} already used by a photo"]
    for text in (caplog.text, passed.text, unchecked.text, json.dumps(details)):
        assert "SENTINEL-OTC" not in text


def test_concurrent_photos_against_one_arrival_use_it_once(
    tmp_path: Path, store: SubmissionStore, db: Db
) -> None:
    sessions = repository(CHALLENGES)
    session = sessions.get_session(UUID(SESSION))
    checkpoint = sessions.get_checkpoint(UUID(SESSION), 1)
    assert session is not None
    assert checkpoint is not None
    participant = store.join_team(UUID(SESSION), "Red Foxes", NOW).participant
    store.arrive(
        UUID(SESSION), participant, 1, pose=POSE, now=NOW, ttl=TTL, new_code=lambda: "1234"
    )
    before = [CheckedInCheck()]
    barrier = Barrier(2)

    def send(seed: int) -> str:
        photo = jpeg(scene(seed))
        metadata = ChallengeMetadata.model_validate(
            {
                "session": SESSION,
                "participant": str(participant),
                "checkpoint": 1,
                "location": {"lat": 51.5001, "long": -0.1},
                "capture-time": NOW.isoformat(),
            }
        )
        arrival = store.latest_arrival(UUID(SESSION), participant, 1, NOW)
        ctx = SubmissionContext(
            metadata, NOW, session, checkpoint, photo, perceptual_hash(photo), arrival=arrival
        )
        earlier = run_checks(before, ctx)  # both see the arrival active
        barrier.wait()
        submission, _, _ = judge_and_record(
            ctx, before, earlier, [], ImageStore(tmp_path), store, Stopwatch(time.monotonic)
        )
        [result] = submission.checks
        return result.outcome

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(send, [1, 2]))

    assert sorted(outcomes) == ["failed", "passed"]
    assert sorted(row["arrival_id"] is None for row in rows(db)) == [False, True]


# --- the check itself ---------------------------------------------------------------

ISSUED = NOW - timedelta(minutes=1)
ARRIVAL = Arrival(42, 1, "SENTINEL-CODE", POSE, issued_at=ISSUED, expires_at=ISSUED + TTL)


def make_ctx(arrival: LatestArrival | None, at: datetime = NOW) -> SubmissionContext:
    checkpoint = Checkpoint(
        sequence=1,
        name="Spot",
        clue="Find it",
        location=Location(lat=0, long=0),
        proximity=10,
        challenge=VisualChallenge(scene=SCENE, pose=NEW_POSE),
    )
    session = GameSession(
        id=UUID(SESSION),
        name="Hunt",
        location="Here",
        start_time=START,
        end_time=START + timedelta(hours=3),
        checkpoints=(checkpoint,),
    )
    metadata = ChallengeMetadata(
        session=session.id,
        participant=UUID(int=2),
        checkpoint=1,
        location=Location(lat=0, long=0),
        capture_time=at,
    )
    return SubmissionContext(metadata, at, session, checkpoint, b"img", 0, arrival=arrival)


@pytest.mark.parametrize(
    ("arrival", "at", "outcome", "rejection", "detail"),
    [
        pytest.param(
            LatestArrival(ARRIVAL, used=False), NOW, "passed", None, "arrival 42", id="active"
        ),
        pytest.param(
            None, NOW, "failed", NOT_CHECKED_IN, "no arrival at this checkpoint", id="never"
        ),
        pytest.param(
            LatestArrival(ARRIVAL, used=False),
            ARRIVAL.expires_at,
            "failed",
            CHECK_IN_EXPIRED,
            "arrival 42 expired at 2026-10-03T09:39:00+00:00",
            id="expired",
        ),
        pytest.param(
            LatestArrival(ARRIVAL, used=True),
            NOW,
            "failed",
            NOT_CHECKED_IN,
            "arrival 42 already used by a photo",
            id="used",
        ),
        pytest.param(
            LatestArrival(ARRIVAL, used=True),
            ARRIVAL.expires_at,
            "failed",
            NOT_CHECKED_IN,
            "arrival 42 already used by a photo",
            id="used-then-expired",
        ),
    ],
)
def test_checked_in_outcomes(
    arrival: LatestArrival | None,
    at: datetime,
    outcome: str,
    rejection: Any,
    detail: str,
) -> None:
    result = CheckedInCheck()(make_ctx(arrival, at))

    assert (result.check, result.outcome, result.confidence) == ("checked_in", outcome, 1.0)
    assert result.rejection == rejection
    assert result.detail == detail
    assert "SENTINEL-CODE" not in f"{result.reason} {result.detail}"


def test_the_challenge_takes_the_arrivals_pose() -> None:
    ctx = make_ctx(LatestArrival(ARRIVAL, used=False))

    assert ctx.active_arrival == ARRIVAL
    assert ctx.challenge == VisualChallenge(scene=SCENE, pose=POSE)


@pytest.mark.parametrize(
    "ctx",
    [
        pytest.param(make_ctx(None), id="no-arrival"),
        pytest.param(make_ctx(LatestArrival(ARRIVAL, used=True)), id="used"),
        pytest.param(
            make_ctx(LatestArrival(replace(ARRIVAL, pose=None), used=False)), id="no-pose"
        ),
        pytest.param(
            replace(
                make_ctx(LatestArrival(ARRIVAL, used=False)),
                checkpoint=make_ctx(None).checkpoint.model_copy(update={"challenge": None}),
            ),
            id="no-challenge",
        ),
    ],
)
def test_nothing_to_judge(ctx: SubmissionContext) -> None:
    assert ctx.challenge is None

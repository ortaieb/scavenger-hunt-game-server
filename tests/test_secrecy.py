"""No endpoint may reveal a checkpoint's scene description (the answer to its clue)."""

import functools
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import anthropic
import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from images import jpeg, scene
from psycopg.rows import DictRow
from pytest_mock import MockerFixture
from starlette.routing import Route

from game_server.app import create_app
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.database import Database
from game_server.designer_runners import StubRunner, get_designer_runner
from game_server.drafts import DraftStore
from game_server.models import VerdictStatus
from game_server.referee import ClaudeReferee, ModelReply, get_referee, system_prompt
from game_server.sessions import get_session_repository, parse_sessions
from game_server.submissions import NewSubmission, SubmissionStore

SENTINEL = "SENTINEL-SCENE-4b1d"
REASON = "SENTINEL-REASON-8f"  # the model's description of the photo
JOIN_CODE = "SENTINEL-CODE-9X"  # a credential: no response may echo it
NAME = "SENTINEL-NAME-c3"  # checkpoint names are never shown
LATER_CLUE = "SENTINEL-LATER-CLUE-7e"  # the second clue on the route: not while on the first
PHOTO_NAME = "SENTINEL-PHOTO-fountain-north"  # a reference photo's file name shows the place
MODERATOR_CODE = "SENTINEL-MOD-4Q"  # a credential: never in a response or a log line
RIVALS = "SENTINEL-RIVALS-5d"  # another team: never in this team's state
RIVAL_CODE = "SENTINEL-RIVAL-CODE-2W"
ORGANISER_KEY = "SENTINEL-ORGANISER-KEY-3b9d"  # a credential: never in a response or a log
NOTE = "SENTINEL-NOTE-6c"  # the moderator's note on a ruling may describe the photo: never logged
SESSION = "aeffe667-4f9f-4108-b5e2-56ae821fe413"
UNKNOWN = "0b5e9c1e-2f7a-4d8e-9a57-3c1f6f0d2b44"
NOW = datetime(2026, 10, 3, 10, 30, tzinfo=UTC)
PHOTO = jpeg(scene(3))
# The model is unsure, so a photo that passes the other checks is pending.
MODEL_OUTPUT = json.dumps(
    {
        "scene_matches": {"reason": f"{REASON} a fountain", "verdict": "unsure", "confidence": 0.5},
        "pose_correct": {"reason": f"{REASON} a wave", "verdict": "unsure", "confidence": 0.5},
    }
)

SESSIONS_JSON = json.dumps(
    [
        {
            "id": SESSION,
            "name": "Hunt",
            "location": "Here",
            "start-time": "2026-10-03T10:00:00Z",
            "end-time": "2026-10-03T12:00:00Z",
            "checkpoints": [
                {
                    "sequence": 1,
                    "name": NAME,
                    "clue": "Find it",
                    "location": {"lat": 51.5, "long": -0.1},
                    "proximity": 40,
                    "challenge": {"scene": f"{SENTINEL} a fountain", "pose": "Wave."},
                    "reference-photos": [f"reference/{PHOTO_NAME}.jpg"],
                },
                {
                    "sequence": 2,
                    "name": "Second spot",
                    "clue": LATER_CLUE,
                    "location": {"lat": 51.6, "long": -0.2},
                    "proximity": 40,
                },
            ],
            "teams": [
                {"name": "Testers", "join-code": JOIN_CODE, "order": [1, 2]},
                {"name": RIVALS, "join-code": RIVAL_CODE, "order": [2, 1]},
            ],
            "moderator-code": MODERATOR_CODE,
        }
    ]
)


@pytest.fixture
def designer(database: Database) -> Iterator[StubRunner]:
    """The stub designer, waiting until the test wakes it."""
    runner = StubRunner(DraftStore(database), lambda: NOW, delay_seconds=60)
    yield runner
    runner.stop()


@pytest.fixture
def client(tmp_path: Path, mocker: MockerFixture, designer: StubRunner) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_designer_runner] = lambda: designer
    # The real referee, with only the SDK call faked: its traces hold the scene and reasons.
    reply = ModelReply("end_turn", MODEL_OUTPUT, "claude-haiku-4-5", "req_secrecy", 1500, 120)
    mocker.patch("game_server.referee._create_structured_message", return_value=reply)
    sdk = anthropic.Anthropic(api_key="test-key-not-used")
    referee = ClaudeReferee(sdk, "claude-haiku-4-5", max_image_edge=1568)
    app.dependency_overrides[get_referee] = lambda: referee
    settings = Settings(
        image_base_path=tmp_path / "images", organiser_key=ORGANISER_KEY, designer_runner="stub"
    )
    app.dependency_overrides[get_settings] = lambda: settings
    (tmp_path / "reference").mkdir()
    (tmp_path / "reference" / f"{PHOTO_NAME}.jpg").write_bytes(jpeg(scene(4, (64, 48))))
    repository = parse_sessions(SESSIONS_JSON, reference_dir=tmp_path)
    assert repository.reference_photos(UUID(SESSION), 1)  # really loaded
    app.dependency_overrides[get_session_repository] = lambda: repository
    app.dependency_overrides[get_clock] = lambda: lambda: NOW
    with TestClient(app) as test_client:
        yield test_client


def metadata(**changes: Any) -> str:
    return json.dumps(
        {
            "session": SESSION,
            "participant": "7c860ccc-9adf-4e22-b54f-3ff158f5d600",
            "checkpoint": 1,
            "location": {"lat": 51.5001, "long": -0.1},
            "capture-time": "2026-10-03T10:29:00Z",
            **changes,
        }
    )


def submit(client: TestClient, raw_metadata: str, image: bytes = PHOTO) -> Response:
    return client.post(
        "/challenge",
        files={
            "metadata": (None, raw_metadata, "application/json"),
            "challenge-image": ("photo.jpeg", image, "image/jpeg"),
        },
    )


def hint(client: TestClient, **changes: Any) -> Response:
    body = {
        "session": SESSION,
        "participant": "5d0a8b8e-7f6c-4d4b-8f0e-2b1a9c3d4e5f",
        "checkpoint": 1,
        "location": {"lat": 51.5001, "long": -0.1},
        **changes,
    }
    return client.post("/checkpoint/proximity", json=body)


def every_route_response(
    client: TestClient, designer: StubRunner
) -> dict[tuple[str, str], list[Response]]:
    """Successful and failing calls to every route, keyed by (method, route path)."""
    state = "/sessions/{session}/participants/{participant}/state"
    arrive = "/sessions/{session}/participants/{participant}/arrive"
    joined = client.post("/join", json={"code": JOIN_CODE, "consent": True})  # 201, scheduled
    participant = joined.json()["participant"]
    opening = session_opening_responses(client)  # the session runs from here on
    states = [  # while on its first checkpoint: before the photos complete it
        client.get(f"/sessions/{SESSION}/participants/{participant}/state"),
        client.get(f"/sessions/{SESSION}/participants/{UNKNOWN}/state"),  # 404
        client.get(f"/sessions/{UNKNOWN}/participants/{participant}/state"),  # 404
        client.get(f"/sessions/{SESSION}/participants/not-a-uuid/state"),  # 422
    ]
    arrivals = [  # the check-in comes before the photo
        client.post(f"/sessions/{SESSION}/participants/{participant}/arrive", json=body)
        for body in (
            {"checkpoint": 1},  # 201
            {"checkpoint": 1},  # 200: the active arrival
            {"checkpoint": 9},  # 404 unknown checkpoint
            {"checkpoint": 2},  # 409 not the team's current checkpoint
            {"checkpoint": "1"},  # 422
        )
    ]
    photo = functools.partial(metadata, participant=participant)
    responses = {
        ("GET", "/"): [client.get("/")],
        ("POST", "/challenge"): [
            submit(client, photo()),  # pending
            submit(client, photo(location={"lat": 51.51, "long": -0.1})),  # failed checks
            submit(client, photo(), image=PHOTO),  # duplicate
            submit(client, photo(checkpoint=9)),  # 404
            submit(client, metadata(participant=UNKNOWN)),  # 404 unknown participant
            submit(client, photo(extra=True)),  # 422
            submit(client, photo(), image=b"\xff\xd8\xffjunk"),  # 422 undecodable
        ],
        ("POST", "/checkpoint/proximity"): [
            hint(client),  # 200
            hint(client),  # 429
            hint(client, session=UNKNOWN),  # 404
            hint(client, checkpoint="1"),  # 422
        ],
        ("GET", "/health"): [client.get("/health")],
        ("POST", "/join"): [
            joined,
            client.post("/join", json={"code": JOIN_CODE.lower(), "consent": True}),  # 200
            client.post("/join", json={"code": "NO-SUCH-CODE", "consent": True}),  # 404
            client.post("/join", json={"code": JOIN_CODE}),  # 422: consent missing
            client.post("/join", json={"code": JOIN_CODE, "consent": "true"}),  # 422
            client.post("/join", json={"code": JOIN_CODE, "consent": True, "x": 1}),  # 422
        ],
        ("GET", state): states,
        ("POST", arrive): arrivals,
        ("GET", "/openapi.json"): [client.get("/openapi.json")],
        ("GET", "/docs"): [client.get("/docs")],
        ("GET", "/docs/oauth2-redirect"): [client.get("/docs/oauth2-redirect")],
        ("GET", "/redoc"): [client.get("/redoc")],
    }
    closing = session_closing_responses(client)  # the session is stopped from here on
    after_stop = {
        ("POST", "/join"): [client.post("/join", json={"code": JOIN_CODE, "consent": True})],
        ("GET", state): [client.get(f"/sessions/{SESSION}/participants/{participant}/state")],
        ("POST", arrive): [
            client.post(
                f"/sessions/{SESSION}/participants/{participant}/arrive", json={"checkpoint": 1}
            )
        ],
        ("POST", "/challenge"): [submit(client, photo(), image=jpeg(scene(5)))],
        ("POST", "/checkpoint/proximity"): [hint(client, participant=str(UNKNOWN))],
    }
    designing = designer_responses(client, designer)
    for route, route_responses in (
        *opening.items(),
        *closing.items(),
        *after_stop.items(),
        *designing.items(),
    ):
        responses.setdefault(route, []).extend(route_responses)
    return responses


ORGANISER = {"Authorization": f"Bearer {ORGANISER_KEY}"}
DRAFTS = ("POST", "/designer/drafts")
DRAFT_LIST = ("GET", "/designer/drafts")
DRAFT = ("GET", "/designer/drafts/{draft}")


def designer_responses(
    client: TestClient, designer: StubRunner
) -> dict[tuple[str, str], list[Response]]:
    """The organiser's designer: a draft started, refused, followed and read."""
    body = {"area": "Chiswick, London", "theme": "The Thames and brewing history"}
    started = client.post("/designer/drafts", json=body, headers=ORGANISER)  # 202
    draft = started.json()["id"]
    starts = [
        client.post("/designer/drafts", json=body),  # 401
        client.post("/designer/drafts", json={**body, "checkpoints": 2}, headers=ORGANISER),  # 422
        started,
        client.post("/designer/drafts", json=body, headers=ORGANISER),  # 409: one at a time
    ]
    reads = [client.get(f"/designer/drafts/{draft}", headers=ORGANISER)]  # running
    designer.wake()
    designer.join()
    reads += [
        client.get(f"/designer/drafts/{draft}", headers=ORGANISER),  # ready, with the draft
        client.get(f"/designer/drafts/{draft}"),  # 401
        client.get(f"/designer/drafts/{UNKNOWN}", headers=ORGANISER),  # 404
        client.get("/designer/drafts/not-a-uuid", headers=ORGANISER),  # 422
    ]
    assert reads[1].json()["status"] == "ready"  # not vacuous: the full draft was read
    lists = [
        client.get("/designer/drafts", headers=ORGANISER),
        client.get("/designer/drafts", headers={"Authorization": f"Bearer {MODERATOR_CODE}"}),
    ]
    return {DRAFTS: starts, DRAFT_LIST: lists, DRAFT: reads}


def moderator_post(
    client: TestClient, action: str, session: str = SESSION, **kwargs: Any
) -> Response:
    return client.post(f"/sessions/{session}/{action}", **kwargs)


MODERATOR = {"Authorization": f"Bearer {MODERATOR_CODE}"}
START = ("POST", "/sessions/{session}/start")
STOP = ("POST", "/sessions/{session}/stop")
OVERVIEW = ("GET", "/sessions/{session}/overview")
TRACES = ("GET", "/sessions/{session}/traces")
RULING = ("POST", "/sessions/{session}/submissions/{submission}/ruling")
REVIEW = ("GET", "/sessions/{session}/review")
SUBMISSION_PHOTO = ("GET", "/sessions/{session}/submissions/{submission}/photo")
REFERENCE_PHOTO = ("GET", "/sessions/{session}/checkpoints/{sequence}/reference-photos/{position}")
# Routes only the moderator can call, which name checkpoints on purpose.
NAMES_CHECKPOINTS = {OVERVIEW, REVIEW}
# Routes only the moderator can call, whose 200 shows the scene and the model's reasons on
# purpose (the referee's traces, and the photos waiting for a ruling).
SHOWS_JUDGING = {TRACES, REVIEW}
# Routes that name the traces' `references` field (the reference photos the referee was sent),
# which says nothing about any checkpoint's photos.
NAMES_REFERENCES_FIELD = {TRACES}
# Routes that mention reference photos on purpose once the moderator code is accepted: the
# review (how many a checkpoint has), the reference photos themselves, and the schema
# documenting them. None names one by its file.
NAMES_REFERENCE_PHOTOS = {REVIEW, REFERENCE_PHOTO, ("GET", "/openapi.json")}
# The only routes that serve photos, and only to the moderator.
SERVES_PHOTOS = {SUBMISSION_PHOTO, REFERENCE_PHOTO}


def session_opening_responses(client: TestClient) -> dict[tuple[str, str], list[Response]]:
    """The moderator's calls up to and including starting the session."""
    stop_early = moderator_post(client, "stop", headers=MODERATOR)  # 409 not started
    start_anonymous = moderator_post(client, "start")  # 401
    start = moderator_post(client, "start", headers=MODERATOR)  # 201
    start_again = moderator_post(client, "start", headers=MODERATOR)  # 200
    unknown = moderator_post(client, "start", session=UNKNOWN, headers=MODERATOR)  # 404
    return {
        START: [start_anonymous, start, start_again, unknown],
        STOP: [stop_early],
    }


def session_closing_responses(client: TestClient) -> dict[tuple[str, str], list[Response]]:
    """The moderator stopping the session, and what follows."""
    stop = moderator_post(client, "stop", headers=MODERATOR)  # 201
    stop_again = moderator_post(client, "stop", headers=MODERATOR)  # 200
    start_late = moderator_post(client, "start", headers=MODERATOR)  # 409 stopped
    client.post("/join", json={"code": JOIN_CODE, "consent": True})  # a blocked attempt

    def overview(session: str = SESSION, **kwargs: Any) -> Response:
        return client.get(f"/sessions/{session}/overview", **kwargs)

    overviews = [
        overview(headers=MODERATOR),  # 200, with a standing, progress and a blocked attempt
        overview(),  # 401
        overview(headers={"Authorization": f"Bearer {JOIN_CODE}"}),  # 401: not a moderator
        overview(UNKNOWN, headers=MODERATOR),  # 404
    ]

    def rule(submission: int, session: str = SESSION, **kwargs: Any) -> Response:
        return client.post(f"/sessions/{session}/submissions/{submission}/ruling", **kwargs)

    def traces(session: str = SESSION, **kwargs: Any) -> Response:
        return client.get(f"/sessions/{session}/traces", **kwargs)

    items = traces(headers=MODERATOR).json()["items"]
    pending = next(item["submission"] for item in items if item["verdict"] == "pending")
    reviewing = review_responses(client, pending)  # before it's ruled on
    approve = {"ruling": "approve", "note": f"{NOTE} the arm is cropped"}
    rulings = [
        rule(pending, headers=MODERATOR, json=approve),  # 201
        rule(pending, headers=MODERATOR, json={"ruling": "reject", "note": NOTE}),  # 200
        rule(pending, json=approve),  # 401
        rule(pending, headers={"Authorization": f"Bearer {JOIN_CODE}"}, json=approve),  # 401
        rule(pending, UNKNOWN, headers=MODERATOR, json=approve),  # 404 unknown session
        rule(10**6, headers=MODERATOR, json=approve),  # 404 unknown submission
        rule(pending, headers=MODERATOR, json={"ruling": "maybe"}),  # 422
    ]
    reviewing[REVIEW].append(client.get(f"/sessions/{SESSION}/review", headers=MODERATOR))

    traced = [
        traces(headers=MODERATOR),  # 200, with the referee's calls and a ruling
        traces(),  # 401
        traces(headers={"Authorization": f"Bearer {JOIN_CODE}"}),  # 401: not a moderator
        traces(UNKNOWN, headers=MODERATOR),  # 404
        traces(headers=MODERATOR, params={"limit": 0}),  # 422
    ]
    return {
        START: [start_late],
        STOP: [stop, stop_again],
        OVERVIEW: overviews,
        RULING: rulings,
        TRACES: traced,
        **reviewing,
    }


def review_responses(client: TestClient, pending: int) -> dict[tuple[str, str], list[Response]]:
    """The moderator's review queue, the `pending` photo in it, and a reference photo."""

    def get(path: str, session: str = SESSION, **kwargs: Any) -> Response:
        return client.get(f"/sessions/{session}/{path}", **kwargs)

    not_a_moderator = {"Authorization": f"Bearer {JOIN_CODE}"}
    photo = f"submissions/{pending}/photo"
    reference = "checkpoints/1/reference-photos/0"
    return {
        REVIEW: [
            get("review", headers=MODERATOR),  # 200, with the photo to rule on
            get("review"),  # 401
            get("review", headers=not_a_moderator),  # 401
            get("review", UNKNOWN, headers=MODERATOR),  # 404
        ],
        SUBMISSION_PHOTO: [
            get(photo, headers=MODERATOR),  # 200
            get(photo),  # 401
            get(photo, headers=not_a_moderator),  # 401
            get(photo, UNKNOWN, headers=MODERATOR),  # 404 unknown session
            get(f"submissions/{10**6}/photo", headers=MODERATOR),  # 404 unknown submission
            get("submissions/abc/photo", headers=MODERATOR),  # 422
        ],
        REFERENCE_PHOTO: [
            get(reference, headers=MODERATOR),  # 200
            get(reference),  # 401
            get(reference, headers=not_a_moderator),  # 401
            get(reference, UNKNOWN, headers=MODERATOR),  # 404 unknown session
            get("checkpoints/1/reference-photos/1", headers=MODERATOR),  # 404 past the last
            get("checkpoints/2/reference-photos/0", headers=MODERATOR),  # 404 none there
            get("checkpoints/9/reference-photos/0", headers=MODERATOR),  # 404 unknown checkpoint
            get("checkpoints/1/reference-photos/-1", headers=MODERATOR),  # 422
        ],
    }


def app_routes(client: TestClient) -> set[tuple[str, str]]:
    """Every (method, path) the app serves.

    API routes come from the OpenAPI schema (public, and it sees into included routers);
    the docs routes are excluded from the schema, so they come from the top-level routes.
    """
    app: FastAPI = client.app  # type: ignore[assignment]  # TestClient wraps our FastAPI app
    api = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }
    top_level = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, Route)
        for method in (route.methods or set()) - {"HEAD"}
    }
    return api | top_level


def test_no_route_ever_returns_the_scene(
    client: TestClient,
    designer: StubRunner,
    caplog: pytest.LogCaptureFixture,
    db: psycopg.Connection[DictRow],
) -> None:
    caplog.set_level(logging.DEBUG)
    responses = every_route_response(client, designer)

    assert set(responses) == app_routes(client), "a route is missing from this test"
    for route, route_responses in responses.items():
        for response in route_responses:
            if not (route in SHOWS_JUDGING and response.status_code == 200):
                assert SENTINEL not in response.text, f"{route} leaked the scene"
                assert REASON not in response.text, f"{route} leaked the model's description"
            assert JOIN_CODE not in response.text, f"{route} leaked a join code"
            if route not in NAMES_CHECKPOINTS:
                assert NAME not in response.text, f"{route} leaked a checkpoint name"
            assert LATER_CLUE not in response.text, f"{route} leaked a later clue"
            assert PHOTO_NAME not in response.text, f"{route} leaked a reference photo"
            text = response.text.lower()
            if route == TRACES and response.status_code == 200:
                # The referee's prompt explains reference photos in general, not a checkpoint's.
                body = response.json()
                assert set(body.pop("prompts").values()) == {system_prompt()}
                text = json.dumps(body).lower()
            if route in NAMES_REFERENCES_FIELD:
                text = text.replace('"references"', "")
            if route not in NAMES_REFERENCE_PHOTOS or response.status_code == 401:
                assert "reference" not in text, f"{route} mentions reference photos"
            content_type = response.headers.get("content-type", "")
            if route in SERVES_PHOTOS and response.status_code == 200:
                assert content_type == "image/jpeg"
            else:
                assert not content_type.startswith("image/"), f"{route} served a photo"
            assert MODERATOR_CODE not in response.text, f"{route} leaked the moderator code"
            assert ORGANISER_KEY not in response.text, f"{route} leaked the organiser key"
    for secret in (MODERATOR_CODE, JOIN_CODE, ORGANISER_KEY):
        assert secret not in caplog.text.upper(), "a credential reached the logs"
    assert SENTINEL not in caplog.text, "the scene reached the logs"
    assert NOTE not in caplog.text, "a ruling's note reached the logs"
    assert REASON not in caplog.text, "the model's description reached the logs"
    # Not vacuous: the referee was consulted, and its traces hold both.
    traced = db.execute("SELECT user_text, response_text, judgement FROM referee_traces").fetchall()
    assert traced
    for trace in traced:
        assert SENTINEL in trace["user_text"]
        assert REASON in trace["response_text"]
        assert REASON in trace["judgement"]["scene_matches"]["reason"]


def test_the_calls_cover_success_and_error_paths(client: TestClient, designer: StubRunner) -> None:
    statuses = {
        route: sorted({r.status_code for r in rs})
        for route, rs in every_route_response(client, designer).items()
    }

    assert statuses[("POST", "/challenge")] == [200, 202, 404, 422]
    assert statuses[("POST", "/checkpoint/proximity")] == [200, 404, 422, 429]
    assert statuses[("POST", "/join")] == [200, 201, 404, 409, 422]
    assert statuses[("POST", "/sessions/{session}/start")] == [200, 201, 401, 404, 409]
    assert statuses[("POST", "/sessions/{session}/stop")] == [200, 201, 409]
    assert statuses[OVERVIEW] == [200, 401, 404]
    assert statuses[DRAFTS] == [202, 401, 409, 422]
    assert statuses[DRAFT_LIST] == [200, 401]
    assert statuses[DRAFT] == [200, 401, 404, 422]
    assert statuses[TRACES] == [200, 401, 404, 422]
    assert statuses[RULING] == [200, 201, 401, 404, 422]
    assert statuses[REVIEW] == [200, 401, 404]
    assert statuses[SUBMISSION_PHOTO] == [200, 401, 404, 422]
    assert statuses[REFERENCE_PHOTO] == [200, 401, 404, 422]
    assert statuses[("POST", "/sessions/{session}/participants/{participant}/arrive")] == [
        200,
        201,
        404,
        409,
        422,
    ]
    assert statuses[("GET", "/sessions/{session}/participants/{participant}/state")] == [
        200,
        404,
        422,
    ]


def test_arrive_reveals_no_place(client: TestClient, designer: StubRunner) -> None:
    arrive_responses = every_route_response(client, designer)[
        ("POST", "/sessions/{session}/participants/{participant}/arrive")
    ]

    for response in arrive_responses:
        for leak in ("51.5", "-0.1", "proximity", NAME, SENTINEL, LATER_CLUE):
            assert leak not in response.text


def test_no_route_gives_the_pose_before_arrival(client: TestClient) -> None:
    # The pose is given only at check-in: the old pose endpoint is an unknown path.
    response = client.get(f"/sessions/{SESSION}/checkpoints/1/challenge")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def record(
    store: SubmissionStore, participant: str, checkpoint: int, verdict: VerdictStatus, at: datetime
) -> int:
    return store.record(
        NewSubmission(
            session=UUID(SESSION),
            participant=UUID(participant),
            checkpoint=checkpoint,
            received_at=at,
            capture_time=at,
            lat=51.5,
            long=-0.1,
            image_id=uuid4(),
            verdict=verdict,
            checks=(),
            distance_m=1.0,
            phash=checkpoint,
            processing_ms=40,
        )
    ).id


def test_state_reveals_only_the_teams_own_score(client: TestClient, store: SubmissionStore) -> None:
    """Never another team's points, a per-checkpoint breakdown or a place at one checkpoint."""
    moderator = {"Authorization": f"Bearer {MODERATOR_CODE}"}
    client.post(f"/sessions/{SESSION}/start", headers=moderator)
    testers, rivals = (
        client.post("/join", json={"code": code, "consent": True}).json()["participant"]
        for code in (JOIN_CODE, RIVAL_CODE)
    )
    record(store, rivals, 1, "pass", NOW)  # rivals first at 1: Testers are 2nd there
    record(store, testers, 1, "pass", NOW + timedelta(minutes=1))
    pending = record(store, testers, 2, "pending", NOW + timedelta(minutes=2))
    url = f"/sessions/{SESSION}/participants/{testers}/state"
    states = [client.get(url)]
    client.post(f"/sessions/{SESSION}/stop", headers=moderator)
    states.append(client.get(url))  # not final: the pending photo isn't ruled on yet
    client.post(
        f"/sessions/{SESSION}/submissions/{pending}/ruling",
        headers=moderator,
        json={"ruling": "reject"},
    )
    states.append(client.get(url))

    for response in states:
        body = response.json()
        assert set(body) == {"status", "team", "progress", "current", "session", "score"}
        assert set(body["score"]) == {"points", "in-review", "final", "place"}
        assert set(body["session"]) == {
            "phase",
            "planned-start",
            "planned-end",
            "started-at",
            "stopped-at",
            "server-time",
        }
        for leak in (RIVALS, RIVAL_CODE, rivals, MODERATOR_CODE, JOIN_CODE):
            assert leak not in response.text
    # Testers: 2nd at checkpoint 1, pending then rejected at 2 (3 = two teams joined + 1).
    # Rivals: 1 + 3.
    assert [r.json()["score"] for r in states] == [
        {"points": 5, "in-review": 1, "final": False, "place": None},
        {"points": 5, "in-review": 1, "final": False, "place": None},
        {"points": 5, "in-review": 0, "final": True, "place": 2},
    ]


def test_the_overview_reveals_no_coordinates_clues_or_scenes(client: TestClient) -> None:
    client.post(f"/sessions/{SESSION}/start", headers=MODERATOR)
    testers = client.post("/join", json={"code": JOIN_CODE, "consent": True}).json()
    client.post(
        f"/sessions/{SESSION}/participants/{testers['participant']}/arrive", json={"checkpoint": 1}
    )
    submit(client, metadata(participant=testers["participant"]))  # pending at checkpoint 1

    shown = client.get(f"/sessions/{SESSION}/overview", headers=MODERATOR)

    [team, _] = shown.json()["teams"]
    assert team["last-completed"]["name"] == NAME  # names are for the moderator
    assert team["current"]["name"] == "Second spot"
    for leak in ("51.5", "51.6", "-0.1", "-0.2", "proximity", "Find it", LATER_CLUE, SENTINEL):
        assert leak not in shown.text
    for secret in (JOIN_CODE, MODERATOR_CODE, testers["participant"]):
        assert secret not in shown.text


def test_only_the_moderator_sees_the_judging(client: TestClient) -> None:
    """The traces show the scene and the model's reasons, but no place, code or participant."""
    client.post(f"/sessions/{SESSION}/start", headers=MODERATOR)
    testers = client.post("/join", json={"code": JOIN_CODE, "consent": True}).json()
    client.post(
        f"/sessions/{SESSION}/participants/{testers['participant']}/arrive", json={"checkpoint": 1}
    )
    submit(client, metadata(participant=testers["participant"]))  # judged by the referee
    url = f"/sessions/{SESSION}/traces"

    shown = client.get(url, headers=MODERATOR)
    refused = [client.get(url), client.get(url, headers={"Authorization": f"Bearer {JOIN_CODE}"})]

    assert SENTINEL in shown.text  # the scene, in the call's user text
    assert REASON in shown.text  # the model's reasons, in the judgement and the checks' detail
    for leak in ("51.5", "51.6", "-0.1", "-0.2", "proximity", "Find it", LATER_CLUE, NAME):
        assert leak not in shown.text
    for secret in (JOIN_CODE, MODERATOR_CODE, PHOTO_NAME, testers["participant"]):
        assert secret not in shown.text
    for response in refused:
        assert response.status_code == 401
        assert SENTINEL not in response.text
        assert REASON not in response.text


def test_the_review_reveals_no_coordinates_codes_or_participants(client: TestClient) -> None:
    """The review shows the scene, the pose and the referee's reasons, but no place or code."""
    client.post(f"/sessions/{SESSION}/start", headers=MODERATOR)
    testers = client.post("/join", json={"code": JOIN_CODE, "consent": True}).json()
    arrival = client.post(
        f"/sessions/{SESSION}/participants/{testers['participant']}/arrive", json={"checkpoint": 1}
    ).json()
    submit(client, metadata(participant=testers["participant"]))  # pending at checkpoint 1
    url = f"/sessions/{SESSION}/review"

    shown = client.get(url, headers=MODERATOR)
    refused = [client.get(url), client.get(url, headers={"Authorization": f"Bearer {JOIN_CODE}"})]

    [photo] = shown.json()["to-review"]
    assert photo["checkpoint"]["name"] == NAME  # names are for the moderator
    assert SENTINEL in photo["scene"]
    assert any(REASON in (check["detail"] or "") for check in photo["checks"])
    for leak in ("51.5", "51.6", "-0.1", "-0.2", "proximity", "Find it", LATER_CLUE, PHOTO_NAME):
        assert leak not in shown.text
    for secret in (JOIN_CODE, MODERATOR_CODE, testers["participant"], f'"{arrival["code"]}"'):
        assert secret not in shown.text
    for response in refused:
        assert response.status_code == 401
        assert SENTINEL not in response.text
        assert REASON not in response.text


def test_only_the_moderator_gets_the_photos(client: TestClient) -> None:
    client.post(f"/sessions/{SESSION}/start", headers=MODERATOR)
    testers = client.post("/join", json={"code": JOIN_CODE, "consent": True}).json()
    client.post(
        f"/sessions/{SESSION}/participants/{testers['participant']}/arrive", json={"checkpoint": 1}
    )
    submitted = submit(client, metadata(participant=testers["participant"]))
    assert submitted.status_code == 202
    [photo] = client.get(f"/sessions/{SESSION}/review", headers=MODERATOR).json()["to-review"]
    urls = [
        f"/sessions/{SESSION}/submissions/{photo['submission']}/photo",
        f"/sessions/{SESSION}/checkpoints/1/reference-photos/0",
    ]

    for url in urls:
        assert client.get(url, headers=MODERATOR).headers["content-type"] == "image/jpeg"
        for headers in ({}, {"Authorization": f"Bearer {JOIN_CODE}"}):
            refused = client.get(url, headers=headers)
            assert refused.status_code == 401
            assert refused.headers["content-type"] == "application/json"

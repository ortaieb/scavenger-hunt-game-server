"""The organiser's review of a draft: edit, accept or reject checkpoints, then publish."""

import json
import logging
import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from pytest_mock import MockerFixture

from game_server.app import create_app
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.database import Database
from game_server.designer.review import (
    JOIN_WORDS,
    UNAMBIGUOUS,
    SecretCodes,
    get_code_source,
    issue_codes,
    rotation,
)
from game_server.designer_runners import stub_area, stub_checkpoints
from game_server.drafts import (
    DraftChallenge,
    DraftCheckpoint,
    DraftPlace,
    DraftRequest,
    DraftResult,
    DraftStore,
)
from game_server.models import Location
from game_server.published_sessions import PublishedSessionRows
from game_server.sessions import (
    SessionPublishError,
    SessionRepository,
    get_session_repository,
    parse_sessions,
)

KEY = "SENTINEL-ORGANISER-KEY-7c1e9a"
AUTH = {"Authorization": f"Bearer {KEY}"}
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
FILE_SESSION = "aeffe667-4f9f-4108-b5e2-56ae821fe413"
FILE_CODE = "FILE-FOX-1"
HUNT = {
    "name": "Chiswick river hunt",
    "start-time": "2026-10-11T10:00:00+01:00",
    "end-time": "2026-10-11T12:00:00+01:00",
    "teams": ["Red Foxes", "Blue Herons"],
}
CODE_FORMAT = re.compile(r"^[A-Za-z0-9-]{6,32}$")


def file_sessions() -> SessionRepository:
    checkpoint = {"name": "Spot", "clue": "Find it", "location": {"lat": 51.5, "long": -0.1}}
    return parse_sessions(
        json.dumps(
            [
                {
                    "id": FILE_SESSION,
                    "name": "File hunt",
                    "location": "Here",
                    "start-time": "2026-10-03T09:00:00Z",
                    "end-time": "2026-10-03T12:00:00Z",
                    "checkpoints": [{**checkpoint, "sequence": 1, "proximity": 40}],
                    "teams": [{"name": "Testers", "join-code": FILE_CODE, "order": [1]}],
                }
            ]
        )
    )


@dataclass
class FixedCodes:
    """Hands out queued codes first, then fresh ones: forces a clash."""

    joins: list[str] = field(default_factory=list)
    moderators: list[str] = field(default_factory=list)
    fresh: SecretCodes = field(default_factory=SecretCodes)

    def join_code(self) -> str:
        return self.joins.pop(0) if self.joins else self.fresh.join_code()

    def moderator_code(self) -> str:
        return self.moderators.pop(0) if self.moderators else self.fresh.moderator_code()


@pytest.fixture
def repository(database: Database) -> SessionRepository:
    return file_sessions().beside(PublishedSessionRows(database))


@pytest.fixture
def store(database: Database) -> DraftStore:
    return DraftStore(database)


@pytest.fixture
def codes() -> FixedCodes:
    return FixedCodes()


@pytest.fixture
def client(repository: SessionRepository, codes: FixedCodes) -> Iterator[TestClient]:
    app = create_app()
    settings = Settings(organiser_key=KEY)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: repository
    app.dependency_overrides[get_clock] = lambda: lambda: NOW
    app.dependency_overrides[get_code_source] = lambda: codes
    with TestClient(app) as test_client:
        yield test_client


FOURTH = DraftCheckpoint(
    position=4,
    place=DraftPlace(
        osm="way/9000000004",
        name="Stub Old Boathouse",
        kind="historic=building",
        location=Location(lat=51.4870, long=-0.2620),
    ),
    clue="Where oars were stored by the water.",
    challenge=DraftChallenge(scene="A wooden shed with wide doors.", pose="Row an imaginary boat"),
    proximity=50,
    rationale="Another stub place.",
)


def ready_draft(
    store: DraftStore,
    checkpoints: tuple[DraftCheckpoint, ...] | None = None,
    max_walk_km: float = 3,
) -> UUID:
    """A ready draft, as a run would leave it."""
    checkpoints = checkpoints if checkpoints is not None else (*stub_checkpoints(), FOURTH)
    request = DraftRequest.model_validate(
        {
            "area": "Chiswick",
            "theme": "Brewing",
            "checkpoints": max(3, len(checkpoints)),
            "max-walk-km": max_walk_km,
        }
    )
    draft = uuid4()
    store.create(draft, request, "stub", NOW)
    store.finish(
        draft,
        DraftResult(
            status="ready",
            area=stub_area(request),
            checkpoints=checkpoints,
            problems=(),
            model=None,
            turns=0,
            cost_usd=Decimal(0),
            duration_ms=0,
        ),
        NOW,
    )
    return draft


def edit(client: TestClient, draft: UUID, position: int, **body: Any) -> Response:
    return client.patch(f"/designer/drafts/{draft}/checkpoints/{position}", json=body, headers=AUTH)


def review(client: TestClient, draft: UUID, **reviews: str) -> None:
    """`review(client, draft, p1="accepted", ...)`: set each checkpoint's review."""
    for key, value in reviews.items():
        assert edit(client, draft, int(key[1:]), review=value).status_code == 200


def publish(client: TestClient, draft: UUID, **changes: Any) -> Response:
    return client.post(f"/designer/drafts/{draft}/publish", json={**HUNT, **changes}, headers=AUTH)


def read(client: TestClient, draft: UUID) -> dict[str, Any]:
    body: dict[str, Any] = client.get(f"/designer/drafts/{draft}", headers=AUTH).json()
    return body


def checkpoint_at(client: TestClient, draft: UUID, position: int) -> dict[str, Any]:
    checkpoint: dict[str, Any] = read(client, draft)["checkpoints"][position - 1]
    return checkpoint


# --- editing -------------------------------------------------------------------------------------


def test_a_clue_naming_the_place_is_refused_and_nothing_changes(
    client: TestClient, store: DraftStore
) -> None:
    draft = ready_draft(store)
    before = checkpoint_at(client, draft, 1)

    response = edit(client, draft, 1, clue="Find the lantern by the gate.")

    assert response.status_code == 422
    body = response.json()
    assert body["detail"] == "draft problems"
    assert [(p["code"], p["position"]) for p in body["problems"]] == [("names_place", 1)]
    assert checkpoint_at(client, draft, 1) == before


def test_a_valid_edit_is_saved_with_the_original_kept(
    client: TestClient, store: DraftStore
) -> None:
    draft = ready_draft(store)
    agent_clue = checkpoint_at(client, draft, 1)["clue"]

    first = edit(client, draft, 1, clue="Old lamps once lit this way in.")
    second = edit(client, draft, 1, clue="Lamps lit this way in, long ago.", proximity=45)

    assert first.status_code == second.status_code == 200
    saved = checkpoint_at(client, draft, 1)
    assert saved == second.json()
    assert (saved["clue"], saved["proximity"], saved["edited"]) == (
        "Lamps lit this way in, long ago.",
        45,
        True,
    )
    assert saved["original"] == {"clue": agent_clue, "scene": None, "pose": None, "proximity": 40}


def test_scene_and_pose_are_edited_inside_the_challenge(
    client: TestClient, store: DraftStore
) -> None:
    draft = ready_draft(store)
    agent = checkpoint_at(client, draft, 2)["challenge"]

    response = edit(client, draft, 2, scene="A bench by the river wall.", pose="Sit and wave")

    assert response.json()["challenge"] == {
        "scene": "A bench by the river wall.",
        "pose": "Sit and wave",
    }
    assert response.json()["original"] == {
        "clue": None,
        "scene": agent["scene"],
        "pose": agent["pose"],
        "proximity": None,
    }


def test_a_review_alone_isn_t_an_edit(client: TestClient, store: DraftStore) -> None:
    draft = ready_draft(store)

    response = edit(client, draft, 3, review="rejected")

    assert (response.json()["review"], response.json()["edited"], response.json()["original"]) == (
        "rejected",
        False,
        None,
    )


def test_the_same_text_isn_t_an_edit(client: TestClient, store: DraftStore) -> None:
    draft = ready_draft(store)
    clue = checkpoint_at(client, draft, 1)["clue"]

    response = edit(client, draft, 1, clue=clue)

    assert (response.json()["edited"], response.json()["original"]) == (False, None)


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"proximity": 101}, "bad_proximity"),
        ({"scene": "x" * 1001}, "too_long"),
        ({"pose": "  "}, "empty"),
        ({"clue": "Sit on the riverside seat."}, "names_place"),
    ],
)
def test_edits_are_held_to_the_rules(
    client: TestClient, store: DraftStore, body: dict[str, Any], code: str
) -> None:
    draft = ready_draft(store)

    response = edit(client, draft, 2, **body)

    assert response.status_code == 422
    assert code in [p["code"] for p in response.json()["problems"]]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"clue": None},
        {"review": "maybe"},
        {"proximity": "40"},
        {"proximity": 40.0},
        {"name": "Renamed"},
    ],
    ids=["nothing", "null", "bad-review", "string-proximity", "float-proximity", "unknown-field"],
)
def test_bad_edit_bodies_are_422(
    client: TestClient, store: DraftStore, body: dict[str, Any]
) -> None:
    draft = ready_draft(store)

    response = client.patch(f"/designer/drafts/{draft}/checkpoints/1", json=body, headers=AUTH)

    assert response.status_code == 422
    assert "problems" not in response.json()


def test_unknown_checkpoint_and_draft_are_404(client: TestClient, store: DraftStore) -> None:
    draft = ready_draft(store)

    assert edit(client, draft, 9, review="accepted").json() == {"detail": "unknown checkpoint"}
    assert edit(client, uuid4(), 1, review="accepted").json() == {"detail": "unknown draft"}


@pytest.mark.parametrize("status", ["running", "failed", "published"])
def test_only_a_ready_draft_can_be_edited(
    client: TestClient, store: DraftStore, database: Database, status: str
) -> None:
    draft = ready_draft(store)
    with database.transaction() as conn:
        conn.execute("UPDATE hunt_drafts SET status = %s WHERE id = %s", (status, draft))

    response = edit(client, draft, 1, review="accepted")

    assert response.status_code == 409
    assert response.json() == {"detail": "draft can't be edited", "code": "draft_not_editable"}


def test_edits_to_different_checkpoints_at_once_are_both_kept(
    client: TestClient, store: DraftStore
) -> None:
    draft = ready_draft(store)

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(lambda n: edit(client, draft, n, review="accepted"), (1, 2, 3, 4))
        )

    assert [r.status_code for r in responses] == [200] * 4
    assert [c["review"] for c in read(client, draft)["checkpoints"]] == ["accepted"] * 4


# --- publishing ----------------------------------------------------------------------------------


def published_with_one_rejected(client: TestClient, store: DraftStore) -> tuple[UUID, Response]:
    draft = ready_draft(store)
    review(client, draft, p1="accepted", p2="accepted", p3="rejected", p4="accepted")
    return draft, publish(client, draft)


def test_publishing_three_accepted_and_one_rejected(
    client: TestClient, store: DraftStore, repository: SessionRepository
) -> None:
    _, response = published_with_one_rejected(client, store)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"session", "name", "moderator-code", "teams"}
    assert body["name"] == "Chiswick river hunt"
    assert [t["name"] for t in body["teams"]] == ["Red Foxes", "Blue Herons"]
    session = repository.get_session(UUID(body["session"]))
    assert session is not None
    accepted = [c for c in (*stub_checkpoints(), FOURTH) if c.position != 3]
    assert [c.sequence for c in session.checkpoints] == [1, 2, 3]
    assert [c.location for c in session.checkpoints] == [c.place.location for c in accepted]
    assert [c.name for c in session.checkpoints] == [c.place.name for c in accepted]
    assert [c.challenge for c in session.checkpoints if c.challenge] and all(
        not c.reference_photos for c in session.checkpoints
    )
    assert session.location == "Chiswick (stub)"
    assert session.start_time == datetime(2026, 10, 11, 9, 0, tzinfo=UTC)


def test_each_join_code_works_and_teams_start_apart(
    client: TestClient, store: DraftStore, repository: SessionRepository
) -> None:
    _, response = published_with_one_rejected(client, store)
    body = response.json()

    for team in body["teams"]:
        joined = client.post("/join", json={"code": team["join-code"], "consent": True})
        assert joined.status_code == 201
        assert joined.json()["team"] == team["name"]
    session = repository.get_session(UUID(body["session"]))
    assert session is not None
    assert [team.order for team in session.teams] == [(1, 2, 3), (2, 3, 1)]


def test_the_moderator_code_opens_the_overview(client: TestClient, store: DraftStore) -> None:
    _, response = published_with_one_rejected(client, store)
    body = response.json()

    overview = client.get(
        f"/sessions/{body['session']}/overview",
        headers={"Authorization": f"Bearer {body['moderator-code']}"},
    )

    assert overview.status_code == 200
    assert [t["team"] for t in overview.json()["teams"]] == ["Blue Herons", "Red Foxes"]


def test_the_draft_becomes_published(client: TestClient, store: DraftStore) -> None:
    draft, response = published_with_one_rejected(client, store)

    shown = read(client, draft)

    assert shown["status"] == "published"
    assert shown["published"] == {
        "session": response.json()["session"],
        "at": "2026-10-09T12:00:00Z",
    }


def test_the_publication_shows_the_codes_again(client: TestClient, store: DraftStore) -> None:
    draft, response = published_with_one_rejected(client, store)

    again = client.get(f"/designer/drafts/{draft}/publication", headers=AUTH)

    assert again.status_code == 200
    assert again.json() == response.json()


def test_no_publication_until_published(client: TestClient, store: DraftStore) -> None:
    draft = ready_draft(store)

    response = client.get(f"/designer/drafts/{draft}/publication", headers=AUTH)
    unknown = client.get(f"/designer/drafts/{uuid4()}/publication", headers=AUTH)

    assert (response.status_code, response.json()) == (404, {"detail": "not published"})
    assert (unknown.status_code, unknown.json()) == (404, {"detail": "unknown draft"})


@pytest.mark.parametrize(
    ("reviews", "why"),
    [
        ({"p1": "accepted", "p2": "accepted", "p3": "accepted"}, "4 still pending review"),
        (
            {"p1": "accepted", "p2": "accepted", "p3": "rejected", "p4": "rejected"},
            "2 checkpoint(s) accepted",
        ),
    ],
    ids=["one-pending", "two-accepted"],
)
def test_a_draft_that_isn_t_ready_to_publish(
    client: TestClient, store: DraftStore, reviews: dict[str, str], why: str
) -> None:
    draft = ready_draft(store)
    review(client, draft, **reviews)

    response = publish(client, draft)

    assert response.status_code == 409
    assert response.json()["code"] == "draft_not_ready"
    assert why in response.json()["detail"]


def test_a_second_publish_is_409(client: TestClient, store: DraftStore) -> None:
    draft, _ = published_with_one_rejected(client, store)

    response = publish(client, draft)

    assert response.status_code == 409
    assert response.json() == {"detail": "draft is already published", "code": "draft_not_ready"}


@pytest.mark.parametrize("status", ["running", "failed"])
def test_only_a_ready_draft_is_published(
    client: TestClient, store: DraftStore, database: Database, status: str
) -> None:
    draft = ready_draft(store)
    with database.transaction() as conn:
        conn.execute("UPDATE hunt_drafts SET status = %s WHERE id = %s", (status, draft))

    response = publish(client, draft)

    assert (response.status_code, response.json()["code"]) == (409, "draft_not_ready")


def close_to_first() -> DraftCheckpoint:
    first = stub_checkpoints()[0].place.location
    near = Location(lat=first.lat + 0.0005, long=first.long)  # about 55 m away
    return FOURTH.model_copy(update={"place": FOURTH.place.model_copy(update={"location": near})})


def test_the_accepted_checkpoints_are_checked_again(client: TestClient, store: DraftStore) -> None:
    draft = ready_draft(store, (*stub_checkpoints(), close_to_first()))
    review(client, draft, p1="accepted", p2="accepted", p3="accepted", p4="accepted")

    response = publish(client, draft)

    assert response.status_code == 422
    assert [(p["code"], p["position"]) for p in response.json()["problems"]] == [("too_close", 4)]


def test_rejecting_a_checkpoint_takes_it_out_of_the_checks(
    client: TestClient, store: DraftStore
) -> None:
    draft = ready_draft(store, (*stub_checkpoints(), close_to_first()))
    review(client, draft, p1="accepted", p2="accepted", p3="accepted", p4="rejected")

    assert publish(client, draft).status_code == 201


def test_the_route_is_measured_without_the_rejected_ones(
    client: TestClient, store: DraftStore
) -> None:
    # Too long a loop with the far fourth checkpoint; short enough without it.
    draft = ready_draft(store, max_walk_km=1.5)  # loops: 2.07 km with it, 1.32 km without
    review(client, draft, p1="accepted", p2="accepted", p3="accepted", p4="accepted")
    too_long = publish(client, draft)
    review(client, draft, p4="rejected")

    assert [p["code"] for p in too_long.json()["problems"]] == ["route_too_long"]
    assert publish(client, draft).status_code == 201


@pytest.mark.parametrize(
    "changes",
    [
        {"teams": []},
        {"teams": [f"Team {n}" for n in range(11)]},
        {"teams": ["Red Foxes", "red foxes"]},
        {"teams": ["x" * 41]},
        {"teams": ["  "]},
        {"name": ""},
        {"end-time": "2026-10-11T09:00:00+01:00"},
        {"start-time": "2026-10-11T10:00:00"},
        {"moderator-code": "MOD-CHOSEN-1"},
    ],
    ids=[
        "no-teams",
        "eleven-teams",
        "same-name",
        "long-name",
        "blank-name",
        "no-name",
        "ends-before-it-starts",
        "no-offset",
        "unknown-field",
    ],
)
def test_bad_publish_bodies_are_422(
    client: TestClient, store: DraftStore, changes: dict[str, Any]
) -> None:
    draft = ready_draft(store)
    review(client, draft, p1="accepted", p2="accepted", p3="accepted", p4="rejected")

    response = publish(client, draft, **changes)

    assert response.status_code == 422
    assert read(client, draft)["status"] == "ready"


def test_one_to_ten_teams_are_published(client: TestClient, store: DraftStore) -> None:
    draft = ready_draft(store)
    review(client, draft, p1="accepted", p2="accepted", p3="accepted", p4="accepted")
    teams = [f"Team {n}" for n in range(10)]

    body = publish(client, draft, teams=teams).json()

    assert [t["name"] for t in body["teams"]] == teams


# --- codes ---------------------------------------------------------------------------------------


def test_generated_codes_meet_the_sessions_file_format() -> None:
    codes = SecretCodes()

    for _ in range(50):
        join, moderator = codes.join_code(), codes.moderator_code()
        assert CODE_FORMAT.match(join) and CODE_FORMAT.match(moderator)
        word, chars = join.split("-")
        assert word in JOIN_WORDS
        assert len(chars) == 4 and set(chars) <= set(UNAMBIGUOUS)
        assert len(moderator) >= 12
        assert set(moderator.replace("-", "").removeprefix("MOD")) <= set(UNAMBIGUOUS)


def test_issued_codes_are_unused_and_all_different() -> None:
    taken = {"FOX-2222"}
    source = FixedCodes(
        joins=["FOX-2222", "OWL-3333", "OWL-3333", "ELK-4444"], moderators=["OWL-3333"]
    )

    joins, moderator = issue_codes(2, source, lambda code: code in taken)

    assert joins == ["OWL-3333", "ELK-4444"]
    assert moderator not in {*joins, *taken}


def test_issuing_gives_up_after_too_many_clashes() -> None:
    source = FixedCodes(joins=["FOX-2222"] * 30)

    with pytest.raises(RuntimeError, match="no unused code"):
        issue_codes(1, source, lambda code: code == "FOX-2222")


def test_a_clash_with_an_existing_code_is_drawn_again(
    client: TestClient, store: DraftStore, codes: FixedCodes
) -> None:
    codes.joins = [FILE_CODE, "FOX-7Q2K"]
    codes.moderators = [FILE_CODE.lower()]
    draft = ready_draft(store)
    review(client, draft, p1="accepted", p2="accepted", p3="accepted", p4="accepted")

    body = publish(client, draft, teams=["Red Foxes"]).json()

    assert body["teams"] == [{"name": "Red Foxes", "join-code": "FOX-7Q2K"}]
    assert body["moderator-code"].upper() != FILE_CODE


def test_a_code_taken_by_a_racing_publish_is_drawn_again(
    client: TestClient, store: DraftStore, repository: SessionRepository, mocker: MockerFixture
) -> None:
    real = repository.publish_session
    race = mocker.patch.object(
        repository,
        "publish_session",
        side_effect=[SessionPublishError(["teams[0].join-code: already in use"]), real],
    )
    draft = ready_draft(store)
    review(client, draft, p1="accepted", p2="accepted", p3="accepted", p4="accepted")

    response = publish(client, draft)

    assert response.status_code == 201
    assert race.call_count == 2
    first, second = (call.args[0] for call in race.call_args_list)
    assert first.teams[0].join_code != second.teams[0].join_code


def test_rotation() -> None:
    assert [rotation(3, i) for i in range(4)] == [(1, 2, 3), (2, 3, 1), (3, 1, 2), (1, 2, 3)]
    assert rotation(1, 5) == (1,)


# --- logging -------------------------------------------------------------------------------------


def test_one_line_per_publish_without_the_codes(
    client: TestClient, store: DraftStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    draft, response = published_with_one_rejected(client, store)

    body = response.json()
    lines = [r.getMessage() for r in caplog.records if r.name == "game_server.designer.routes"]
    assert lines == [f"Draft {draft} published as session {body['session']} teams 2 checkpoints 3"]
    for code in (body["moderator-code"], *(t["join-code"] for t in body["teams"])):
        assert code not in caplog.text


def test_the_review_routes_need_the_organiser_key(client: TestClient, store: DraftStore) -> None:
    draft = ready_draft(store)
    url = f"/designer/drafts/{draft}"

    responses = [
        client.patch(f"{url}/checkpoints/1", json={"review": "accepted"}),
        client.post(f"{url}/publish", json=HUNT),
        client.get(f"{url}/publication"),
    ]

    for response in responses:
        assert response.status_code == 401
        assert response.json()["code"] == "organiser_unauthorised"

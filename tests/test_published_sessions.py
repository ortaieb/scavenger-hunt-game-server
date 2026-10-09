"""Published sessions: stored in the database beside the sessions file, served the same way."""

import json
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import DictRow
from pytest_mock import MockerFixture

from game_server.app import create_app
from game_server.clock import get_clock
from game_server.database import Database
from game_server.published_sessions import AlreadyPublishedError, PublishedSessionRows
from game_server.sessions import (
    GameSession,
    SessionPublishError,
    SessionRepository,
    _Indexed,
    get_session_repository,
    parse_sessions,
)

START = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
FILE_SESSION = "aeffe667-4f9f-4108-b5e2-56ae821fe413"
FILE_CODE, FILE_MODERATOR = "FILE-FOX-1", "FILE-MOD-1"
FOX, HERON, MODERATOR = "PUB-FOX-7Q", "PUB-HERON-4M", "PUB-MOD-8H3T"


def session_json(
    session_id: str | UUID,
    codes: tuple[str, ...] = (FOX, HERON),
    moderator: str | None = MODERATOR,
    **changes: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(session_id),
        "name": "Published hunt",
        "location": "Chiswick",
        "start-time": START.isoformat(),
        "end-time": (START + timedelta(hours=3)).isoformat(),
        "checkpoints": [
            {
                "sequence": n,
                "name": f"Spot {n}",
                "clue": f"Clue {n}",
                "location": {"lat": 51.49 + n / 1000, "long": -0.26},
                "proximity": 40,
                "challenge": {"scene": f"Scene {n}", "pose": f"Pose {n}"},
            }
            for n in (1, 2, 3)
        ],
        "teams": [
            {"name": name, "join-code": code, "order": order}
            for name, code, order in zip(
                ["Red Foxes", "Blue Herons", "Green Owls"],
                codes,
                [[1, 2, 3], [2, 3, 1], [3, 1, 2]],
                strict=False,
            )
        ],
        **changes,
    }
    if moderator is not None:
        payload["moderator-code"] = moderator
    return payload


def game_session(session_id: str | UUID | None = None, **kwargs: Any) -> GameSession:
    return GameSession.model_validate(session_json(session_id or uuid4(), **kwargs))


@pytest.fixture
def rows(database: Database) -> PublishedSessionRows:
    return PublishedSessionRows(database)


@pytest.fixture
def repository(rows: PublishedSessionRows) -> SessionRepository:
    """The file's sessions (one, with its own codes) beside the database."""
    file_session = session_json(FILE_SESSION, codes=(FILE_CODE,), moderator=FILE_MODERATOR)
    return parse_sessions(json.dumps([file_session])).beside(rows)


def stored(db: psycopg.Connection[DictRow]) -> tuple[int, int]:
    sessions = db.execute("SELECT COUNT(*) AS n FROM published_sessions").fetchone()
    codes = db.execute("SELECT COUNT(*) AS n FROM session_codes").fetchone()
    assert sessions is not None and codes is not None
    return sessions["n"], codes["n"]


# --- publishing and reading back --------------------------------------------------------------


def test_a_published_session_is_served_like_a_file_session(repository: SessionRepository) -> None:
    session = game_session()

    repository.publish_session(session)

    assert repository.get_session(session.id) == session
    assert repository.get_checkpoint(session.id, 2) == session.checkpoints[1]
    assert repository.get_checkpoint(session.id, 9) is None
    assert repository.find_team(f" {FOX.lower()} ") == (session, session.teams[0])
    assert repository.get_team(session.id, "BLUE HERONS") == session.teams[1]
    assert repository.get_team(session.id, "Nobody") is None
    assert repository.moderator_code(session.id) == MODERATOR
    assert repository.reference_photos(session.id, 1) == ()


def test_file_sessions_are_still_served(repository: SessionRepository) -> None:
    file_id = UUID(FILE_SESSION)

    assert repository.get_session(file_id) is not None
    assert repository.moderator_code(file_id) == FILE_MODERATOR
    found = repository.find_team(FILE_CODE)
    assert found is not None
    assert found[0].id == file_id


def test_the_document_is_in_the_sessions_file_shape(
    repository: SessionRepository, db: psycopg.Connection[DictRow]
) -> None:
    session, draft = game_session(), uuid4()

    repository.publish_session(session, draft)

    row = db.execute("SELECT document, draft, published_at FROM published_sessions").fetchone()
    assert row is not None
    assert row["draft"] == draft
    assert row["published_at"] is not None
    reloaded = parse_sessions(json.dumps([row["document"]])).get_session(session.id)
    assert reloaded == session
    kinds = db.execute("SELECT code, kind FROM session_codes ORDER BY code").fetchall()
    assert sorted((r["code"], r["kind"]) for r in kinds) == [
        (FOX, "join"),
        (HERON, "join"),
        (MODERATOR, "moderator"),
    ]


def test_codes_are_stored_normalised(repository: SessionRepository) -> None:
    session = game_session(codes=(" pub-fox-7q ", HERON))

    repository.publish_session(session)

    assert repository.find_team("PUB-FOX-7Q") == (session, session.teams[0])


def test_a_session_without_a_moderator_code(repository: SessionRepository) -> None:
    session = game_session(moderator=None)

    repository.publish_session(session)

    assert repository.moderator_code(session.id) is None


# --- what publishing refuses --------------------------------------------------------------------


def refused(
    repository: SessionRepository, db: psycopg.Connection[DictRow], session: GameSession
) -> list[str]:
    before = stored(db)
    with pytest.raises(SessionPublishError) as raised:
        repository.publish_session(session)
    assert stored(db) == before, "nothing is stored"
    return raised.value.problems


def test_a_join_code_in_the_file(
    repository: SessionRepository, db: psycopg.Connection[DictRow]
) -> None:
    problems = refused(repository, db, game_session(codes=(FOX, FILE_CODE.lower())))

    assert problems == ["teams[1].join-code: already in use"]


def test_a_join_code_already_published(
    repository: SessionRepository, db: psycopg.Connection[DictRow]
) -> None:
    repository.publish_session(game_session())

    second = game_session(codes=("PUB-OWL-9K", FOX), moderator="PUB-MOD-2")

    assert refused(repository, db, second) == ["teams[1].join-code: already in use"]


@pytest.mark.parametrize(
    ("moderator", "path"),
    [(FILE_CODE, "moderator-code"), (FILE_MODERATOR, "moderator-code")],
    ids=["a-file-join-code", "the-file-moderator-code"],
)
def test_a_moderator_code_taken_by_the_file(
    repository: SessionRepository,
    db: psycopg.Connection[DictRow],
    moderator: str,
    path: str,
) -> None:
    problems = refused(repository, db, game_session(moderator=moderator))

    assert problems == [f"{path}: already in use"]


def test_a_moderator_code_equal_to_a_published_join_code(
    repository: SessionRepository, db: psycopg.Connection[DictRow]
) -> None:
    repository.publish_session(game_session())

    problems = refused(repository, db, game_session(codes=("PUB-OWL-9K",), moderator=HERON))

    assert problems == ["moderator-code: already in use"]


def test_a_moderator_code_equal_to_its_own_join_code(
    repository: SessionRepository, db: psycopg.Connection[DictRow]
) -> None:
    problems = refused(repository, db, game_session(moderator=HERON))

    assert problems == ["teams[1].join-code: same as a moderator code"]


def test_a_join_code_used_twice_in_the_session(
    repository: SessionRepository, db: psycopg.Connection[DictRow]
) -> None:
    problems = refused(repository, db, game_session(codes=(FOX, FOX.lower())))

    assert problems == ["teams[1].join-code: duplicate join code"]


def test_an_id_in_use(repository: SessionRepository, db: psycopg.Connection[DictRow]) -> None:
    published = game_session()
    repository.publish_session(published)

    in_the_file = refused(
        repository, db, game_session(FILE_SESSION, codes=("PUB-A-1", "PUB-B-1"), moderator=None)
    )
    published_again = refused(
        repository, db, game_session(published.id, codes=("PUB-C-1",), moderator=None)
    )

    assert in_the_file == ["id: already in use"]
    assert published_again == ["id: already in use"]


def test_the_sessions_file_rules_apply(
    repository: SessionRepository, db: psycopg.Connection[DictRow]
) -> None:
    # model_copy doesn't validate: as if the caller built a session that breaks the rules.
    broken = game_session().model_copy(update={"end_time": START - timedelta(hours=1)})

    problems = refused(repository, db, broken)

    assert problems == ["(root): Value error, end-time must be after start-time"]


def test_a_team_order_is_reported_at_its_path(
    repository: SessionRepository, db: psycopg.Connection[DictRow]
) -> None:
    session = game_session()
    broken_team = session.teams[1].model_copy(update={"order": (2, 2, 1)})
    broken = session.model_copy(update={"teams": (session.teams[0], broken_team)})

    assert refused(repository, db, broken) == ["teams[1].order: repeats checkpoint 2"]


def test_reference_photos_are_refused(
    repository: SessionRepository, db: psycopg.Connection[DictRow]
) -> None:
    payload = session_json(uuid4())
    payload["checkpoints"][1]["reference-photos"] = ["reference/fountain.jpg"]

    problems = refused(repository, db, GameSession.model_validate(payload))

    assert problems == [
        "checkpoints[1].reference-photos: a published session has no reference photos"
    ]


def test_problems_never_show_a_value(
    repository: SessionRepository, db: psycopg.Connection[DictRow]
) -> None:
    repository.publish_session(game_session())

    with pytest.raises(SessionPublishError) as raised:
        repository.publish_session(game_session(codes=(FOX, FILE_CODE), moderator=HERON))

    message = str(raised.value)
    assert message.startswith("3 validation error(s)")
    for code in (FOX, FILE_CODE, HERON):
        assert code not in message.upper()


def test_a_file_only_repository_cant_publish() -> None:
    with pytest.raises(RuntimeError, match="needs the database"):
        SessionRepository().publish_session(game_session())


# --- races -------------------------------------------------------------------------------------


def test_two_publishes_racing_for_one_code(
    rows: PublishedSessionRows, db: psycopg.Connection[DictRow]
) -> None:
    barrier = Barrier(2)

    def publish(other_code: str) -> str:
        repository = SessionRepository().beside(rows)  # as two server processes would
        session = game_session(codes=(FOX, other_code), moderator=f"MOD-{other_code}")
        barrier.wait()
        try:
            repository.publish_session(session)
        except SessionPublishError as exc:
            return "\n".join(exc.problems)
        return "published"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(publish, ["PUB-A-11", "PUB-B-22"]))

    assert outcomes == ["published", "teams[0].join-code: already in use"]
    assert stored(db) == (1, 3)


def test_the_database_refuses_a_code_the_check_missed(
    repository: SessionRepository, db: psycopg.Connection[DictRow], mocker: MockerFixture
) -> None:
    repository.publish_session(game_session())
    real = PublishedSessionRows.codes_in_use
    checks: list[object] = []

    def missed_first(rows: PublishedSessionRows, codes: set[str]) -> set[str]:
        # The first check ran before the other publish committed, so it found nothing.
        checks.append(codes)
        return set() if len(checks) == 1 else real(rows, codes)

    mocker.patch.object(
        PublishedSessionRows, "codes_in_use", autospec=True, side_effect=missed_first
    )

    with pytest.raises(SessionPublishError) as raised:
        repository.publish_session(game_session(codes=("PUB-OWL-9K", FOX), moderator="PUB-MOD-2"))

    assert raised.value.problems == ["teams[1].join-code: already in use"]
    assert len(checks) == 2
    assert stored(db) == (1, 3)


def test_the_rows_refuse_a_taken_code(
    rows: PublishedSessionRows, db: psycopg.Connection[DictRow]
) -> None:
    rows.insert(uuid4(), {}, None, [("CODE-1", "join")])

    with pytest.raises(AlreadyPublishedError):
        rows.insert(uuid4(), {}, None, [("CODE-2", "join"), ("CODE-1", "join")])

    assert stored(db) == (1, 1)


# --- restarts, misses and caching ------------------------------------------------------------


def test_published_sessions_outlive_a_restart(
    repository: SessionRepository, database: Database
) -> None:
    session = game_session()
    repository.publish_session(session)

    restarted = SessionRepository().beside(PublishedSessionRows(database))

    assert restarted.get_session(session.id) == session
    assert restarted.find_team(HERON) == (session, session.teams[1])


def test_an_unknown_id_is_one_query(
    repository: SessionRepository, rows: PublishedSessionRows, mocker: MockerFixture
) -> None:
    document = mocker.spy(rows, "document")
    by_code = mocker.spy(rows, "session_for_code")

    assert repository.get_session(uuid4()) is None

    assert document.call_count == 1
    assert by_code.call_count == 0


def test_an_unknown_code_is_one_query(
    repository: SessionRepository, rows: PublishedSessionRows, mocker: MockerFixture
) -> None:
    by_code = mocker.spy(rows, "session_for_code")
    document = mocker.spy(rows, "document")

    assert repository.find_team("NO-SUCH-CODE") is None

    assert (by_code.call_count, document.call_count) == (1, 0)


def test_a_published_session_is_read_once(
    repository: SessionRepository, rows: PublishedSessionRows, mocker: MockerFixture
) -> None:
    session = game_session()
    SessionRepository().beside(rows).publish_session(session)  # published elsewhere
    document = mocker.spy(rows, "document")
    by_code = mocker.spy(rows, "session_for_code")

    for _ in range(3):
        assert repository.get_session(session.id) == session
        assert repository.find_team(FOX) is not None

    assert (document.call_count, by_code.call_count) == (1, 0)


def test_file_sessions_never_query(
    repository: SessionRepository, rows: PublishedSessionRows, mocker: MockerFixture
) -> None:
    document = mocker.spy(rows, "document")
    by_code = mocker.spy(rows, "session_for_code")
    file_id = UUID(FILE_SESSION)

    repository.get_session(file_id)
    repository.get_checkpoint(file_id, 1)
    repository.get_team(file_id, "Red Foxes")
    repository.moderator_code(file_id)
    repository.find_team(FILE_CODE)

    assert (document.call_count, by_code.call_count) == (0, 0)


# --- secrecy -----------------------------------------------------------------------------------


def test_codes_are_never_in_a_repr_or_a_log(
    repository: SessionRepository, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    repository.publish_session(game_session())
    repository.find_team(FOX)

    indexed = _Indexed.of(game_session())
    for code in (FOX, HERON, MODERATOR, FILE_CODE, FILE_MODERATOR):
        assert code not in repr(repository)
        assert code not in repr(indexed)
        assert code not in caplog.text


# --- playable at once, through the API -----------------------------------------------------------


@pytest.fixture
def client(repository: SessionRepository) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_session_repository] = lambda: repository
    app.dependency_overrides[get_clock] = lambda: lambda: START + timedelta(minutes=5)
    with TestClient(app) as test_client:
        yield test_client


def test_a_published_session_is_playable_at_once(
    repository: SessionRepository, client: TestClient
) -> None:
    session = game_session()
    repository.publish_session(session)

    joined = {
        code: client.post("/join", json={"code": code, "consent": True}).json()
        for code in (FOX, HERON)
    }
    started = client.post(
        f"/sessions/{session.id}/start", headers={"Authorization": f"Bearer {MODERATOR}"}
    )

    assert started.status_code == 201
    for code, first in ((FOX, "Clue 1"), (HERON, "Clue 2")):
        state = client.get(
            f"/sessions/{session.id}/participants/{joined[code]['participant']}/state"
        ).json()
        assert (state["status"], state["current"]["clue"]) == ("playing", first)


def test_an_unknown_session_is_still_404(client: TestClient) -> None:
    response = client.get(f"/sessions/{uuid4()}/overview", headers={"Authorization": "Bearer x"})

    assert response.status_code == 404
    assert response.json() == {"detail": "unknown session"}


def test_the_reference_photo_route_has_none_for_a_published_session(
    repository: SessionRepository, client: TestClient
) -> None:
    session = game_session()
    repository.publish_session(session)

    response = client.get(
        f"/sessions/{session.id}/checkpoints/1/reference-photos/1",
        headers={"Authorization": f"Bearer {MODERATOR}"},
    )

    assert response.status_code == 404

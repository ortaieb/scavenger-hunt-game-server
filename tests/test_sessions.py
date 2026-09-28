import json
from collections.abc import Iterator
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from game_server.config import Settings
from game_server.sessions import (
    Checkpoint,
    GameSession,
    SessionRepository,
    SessionsFileError,
    get_session_repository,
    load_session_repository,
    parse_sessions,
)

SESSION_ID = "aeffe667-4f9f-4108-b5e2-56ae821fe413"
OTHER_SESSION_ID = "0b5e9c1e-2f7a-4d8e-9a57-3c1f6f0d2b44"
EXAMPLE_FILE = Path(__file__).parent.parent / "sessions.example.json"
BST = timezone(timedelta(hours=1))


def session_payload(session_id: str = SESSION_ID) -> dict[str, Any]:
    return {
        "id": session_id,
        "name": "Test hunt",
        "location": "Somewhere",
        "start-time": "2026-10-03T10:00:00+01:00",
        "end-time": "2026-10-03T13:00:00+01:00",
        "checkpoints": [
            {
                "sequence": 1,
                "name": "First",
                "clue": "Look up",
                "location": {"lat": 51.5, "long": -0.1},
                "proximity": 40,
            },
            {
                "sequence": 2,
                "name": "Second",
                "clue": "Look down",
                "location": {"lat": 51.6, "long": -0.2},
                "proximity": 25,
                "window": {
                    "opens-at": "2026-10-03T11:00:00+01:00",
                    "closes-at": "2026-10-03T12:00:00+01:00",
                },
            },
        ],
    }


def to_json(*sessions: dict[str, Any]) -> str:
    return json.dumps(list(sessions))


@pytest.fixture
def repository() -> SessionRepository:
    return parse_sessions(to_json(session_payload()))


@pytest.fixture(autouse=True)
def clear_load_cache() -> Iterator[None]:
    load_session_repository.cache_clear()
    yield
    load_session_repository.cache_clear()


# --- lookups -----------------------------------------------------------------


def test_get_session_by_id(repository: SessionRepository) -> None:
    session = repository.get_session(UUID(SESSION_ID))

    assert session is not None
    assert session.name == "Test hunt"
    assert session.start_time == datetime(2026, 10, 3, 10, tzinfo=BST)


def test_get_unknown_session_returns_none(repository: SessionRepository) -> None:
    assert repository.get_session(UUID(OTHER_SESSION_ID)) is None


def test_get_checkpoint_by_sequence(repository: SessionRepository) -> None:
    checkpoint = repository.get_checkpoint(UUID(SESSION_ID), 2)

    assert checkpoint is not None
    assert checkpoint.name == "Second"
    assert checkpoint.proximity == 25


@pytest.mark.parametrize(
    ("session_id", "sequence"), [(SESSION_ID, 3), (SESSION_ID, 0), (OTHER_SESSION_ID, 1)]
)
def test_get_unknown_checkpoint_returns_none(
    repository: SessionRepository, session_id: str, sequence: int
) -> None:
    assert repository.get_checkpoint(UUID(session_id), sequence) is None


def test_loads_multiple_sessions() -> None:
    repository = parse_sessions(to_json(session_payload(), session_payload(OTHER_SESSION_ID)))

    assert len(repository) == 2
    assert repository.get_session(UUID(OTHER_SESSION_ID)) is not None


def test_empty_list_gives_empty_repository() -> None:
    assert len(parse_sessions("[]")) == 0


def test_sessions_are_immutable(repository: SessionRepository) -> None:
    session = repository.get_session(UUID(SESSION_ID))
    assert session is not None

    with pytest.raises(ValueError, match="frozen"):
        session.name = "changed"  # type: ignore[misc]  # asserting the runtime guard


# --- effective window --------------------------------------------------------


def lookup(repository: SessionRepository, sequence: int) -> tuple[GameSession, Checkpoint]:
    session = repository.get_session(UUID(SESSION_ID))
    checkpoint = repository.get_checkpoint(UUID(SESSION_ID), sequence)
    assert session is not None
    assert checkpoint is not None
    return session, checkpoint


def test_effective_window_is_checkpoint_window_when_set(repository: SessionRepository) -> None:
    session, checkpoint = lookup(repository, 2)

    assert repository.effective_window(session, checkpoint) == (
        datetime(2026, 10, 3, 11, tzinfo=BST),
        datetime(2026, 10, 3, 12, tzinfo=BST),
    )


def test_effective_window_is_session_times_otherwise(repository: SessionRepository) -> None:
    session, checkpoint = lookup(repository, 1)

    assert repository.effective_window(session, checkpoint) == (
        session.start_time,
        session.end_time,
    )


# --- validation --------------------------------------------------------------


def set_path(payload: dict[str, Any], path: tuple[str | int, ...], value: object) -> None:
    target: Any = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        (("end-time",), "2026-10-03T10:00:00+01:00", "end-time must be after start-time"),
        (("end-time",), "2026-10-03T09:00:00+01:00", "end-time must be after start-time"),
        (("start-time",), "2026-10-03T10:00:00", "timezone"),
        (("checkpoints",), [], "at least 1 item"),
        (("checkpoints", 1, "sequence"), 1, "duplicate checkpoint sequence: 1"),
        (("checkpoints", 0, "sequence"), 0, "greater than or equal to 1"),
        (("checkpoints", 0, "proximity"), 0, "greater than 0"),
        (("checkpoints", 0, "proximity"), -5, "greater than 0"),
        (("checkpoints", 0, "location", "lat"), 90.1, "less than or equal to 90"),
        (("checkpoints", 0, "location", "long"), -180.1, "greater than or equal to -180"),
        (("checkpoints", 0, "name"), "", "at least 1 character"),
        (
            ("checkpoints", 1, "window", "opens-at"),
            "2026-10-03T09:59:59+01:00",
            "window must be within",
        ),
        (
            ("checkpoints", 1, "window", "closes-at"),
            "2026-10-03T13:00:01+01:00",
            "window must be within",
        ),
        (
            ("checkpoints", 1, "window", "opens-at"),
            "2026-10-03T12:00:00+01:00",
            "opens-at must be before closes-at",
        ),
        (("organiser",), "someone", "Extra inputs are not permitted"),
        (("checkpoints", 0, "hint"), "extra", "Extra inputs are not permitted"),
        (("checkpoints", 1, "window", "grace"), 5, "Extra inputs are not permitted"),
    ],
)
def test_rejects_invalid_session(path: tuple[str | int, ...], value: object, reason: str) -> None:
    payload = session_payload()
    set_path(payload, path, value)

    with pytest.raises(SessionsFileError, match=reason):
        parse_sessions(to_json(payload))


@pytest.mark.parametrize("field", ["id", "name", "location", "start-time", "end-time"])
def test_rejects_missing_session_field(field: str) -> None:
    payload = session_payload()
    del payload[field]

    with pytest.raises(SessionsFileError, match="Field required"):
        parse_sessions(to_json(payload))


def test_window_may_span_whole_session() -> None:
    payload = session_payload()
    payload["checkpoints"][1]["window"] = {
        "opens-at": payload["start-time"],
        "closes-at": payload["end-time"],
    }

    parse_sessions(to_json(payload))


def test_rejects_duplicate_session_ids() -> None:
    with pytest.raises(SessionsFileError, match=f"duplicate session id: {SESSION_ID}"):
        parse_sessions(to_json(session_payload(), deepcopy(session_payload())))


@pytest.mark.parametrize("raw", ["not json", "{", '{"id": "x"}'])
def test_rejects_malformed_file(raw: str) -> None:
    with pytest.raises(SessionsFileError):
        parse_sessions(raw)


# --- loading from a file -----------------------------------------------------


def test_no_file_configured_gives_empty_repository() -> None:
    assert len(load_session_repository(None)) == 0


def test_loads_file(tmp_path: Path) -> None:
    sessions_file = tmp_path / "sessions.json"
    sessions_file.write_text(to_json(session_payload()))

    repository = load_session_repository(sessions_file)

    assert repository.get_session(UUID(SESSION_ID)) is not None


def test_invalid_file_error_names_the_file(tmp_path: Path) -> None:
    sessions_file = tmp_path / "sessions.json"
    sessions_file.write_text("[{}]")

    with pytest.raises(SessionsFileError, match=f"invalid sessions file {sessions_file}"):
        load_session_repository(sessions_file)


def test_missing_file_is_an_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"

    with pytest.raises(SessionsFileError, match=f"cannot read sessions file {missing}"):
        load_session_repository(missing)


def test_example_file_is_valid() -> None:
    repository = load_session_repository(EXAMPLE_FILE)

    session = repository.get_session(UUID(SESSION_ID))
    assert session is not None
    assert 2 <= len(session.checkpoints) <= 3
    assert any(checkpoint.window for checkpoint in session.checkpoints)


def test_dependency_uses_configured_file(tmp_path: Path) -> None:
    sessions_file = tmp_path / "sessions.json"
    sessions_file.write_text(to_json(session_payload()))

    repository = get_session_repository(Settings(sessions_file=sessions_file))

    assert repository.get_session(UUID(SESSION_ID)) is not None


def test_error_message_gives_path_and_hides_input_values() -> None:
    payload = session_payload()
    payload["checkpoints"][0]["location"]["lat"] = 123.456789

    with pytest.raises(SessionsFileError) as excinfo:
        parse_sessions(to_json(payload))

    message = str(excinfo.value)
    assert "[0].checkpoints[0].location.lat: Input should be less than or equal to 90" in message
    assert "123.456789" not in message
    assert "51.6" not in message  # another checkpoint's coordinates


def test_error_does_not_chain_validation_error_with_inputs() -> None:
    payload = session_payload()
    payload["checkpoints"][0]["proximity"] = 0

    with pytest.raises(SessionsFileError) as excinfo:
        parse_sessions(to_json(payload))

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__

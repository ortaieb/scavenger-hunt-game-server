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
    with_challenge = [c for c in session.checkpoints if c.challenge]
    assert len(with_challenge) >= 2
    assert len(with_challenge) < len(session.checkpoints)  # at least one without


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


# --- visual challenge (#20) --------------------------------------------------

SCENE = "SECRET-SCENE a granite fountain in open lawn"
POSE = "Side profile, looking to your left, with the landmark behind you."


def with_challenge(challenge: object) -> dict[str, Any]:
    payload = session_payload()
    payload["checkpoints"][0]["challenge"] = challenge
    return payload


def test_loads_checkpoint_with_challenge() -> None:
    repository = parse_sessions(to_json(with_challenge({"scene": SCENE, "pose": POSE})))

    checkpoint = repository.get_checkpoint(UUID(SESSION_ID), 1)
    assert checkpoint is not None
    assert checkpoint.challenge is not None
    assert (checkpoint.challenge.scene, checkpoint.challenge.pose) == (SCENE, POSE)


def test_challenge_is_optional() -> None:
    checkpoint = parse_sessions(to_json(session_payload())).get_checkpoint(UUID(SESSION_ID), 1)

    assert checkpoint is not None
    assert checkpoint.challenge is None


@pytest.mark.parametrize(
    ("challenge", "path", "reason"),
    [
        ({"scene": "", "pose": POSE}, "challenge.scene", "at least 1 character"),
        ({"scene": SCENE + "x" * 1000, "pose": POSE}, "challenge.scene", "at most 1000 characters"),
        ({"scene": SCENE, "pose": ""}, "challenge.pose", "at least 1 character"),
        ({"scene": SCENE, "pose": POSE + "x" * 200}, "challenge.pose", "at most 200 characters"),
        ({"pose": POSE}, "challenge.scene", "Field required"),
        ({"scene": SCENE}, "challenge.pose", "Field required"),
        ({"scene": SCENE, "pose": POSE, "hint": "x"}, "challenge.hint", "Extra inputs"),
        ("a string", "challenge", "Input should be an object"),
    ],
)
def test_invalid_challenge_stops_startup_without_echoing_it(
    tmp_path: Path, challenge: object, path: str, reason: str
) -> None:
    sessions_file = tmp_path / "sessions.json"
    sessions_file.write_text(to_json(with_challenge(challenge)))

    with pytest.raises(SessionsFileError) as excinfo:
        load_session_repository(sessions_file)

    message = str(excinfo.value)
    assert f"[0].checkpoints[0].{path}: " in message
    assert reason in message
    assert "SECRET-SCENE" not in message
    assert "landmark" not in message


@pytest.mark.parametrize(("scene_len", "pose_len"), [(1, 1), (1000, 200)])
def test_challenge_length_limits_are_inclusive(scene_len: int, pose_len: int) -> None:
    parse_sessions(to_json(with_challenge({"scene": "s" * scene_len, "pose": "p" * pose_len})))


# --- teams (#36) ---------------------------------------------------------------

CODE_A = "FOX-7Q2K"
CODE_B = "HERON-4MXP"


def with_teams(*teams: dict[str, Any], session_id: str = SESSION_ID) -> dict[str, Any]:
    payload = session_payload(session_id)
    payload["teams"] = list(teams)
    return payload


def team(
    name: str = "Red Foxes", code: str = CODE_A, order: list[int] | None = None
) -> dict[str, Any]:
    return {"name": name, "join-code": code, "order": order if order is not None else [1, 2]}


def test_loads_teams() -> None:
    repository = parse_sessions(to_json(with_teams(team(), team("Blue Herons", CODE_B, [2, 1]))))

    session = repository.get_session(UUID(SESSION_ID))
    assert session is not None
    assert [(t.name, t.order) for t in session.teams] == [
        ("Red Foxes", (1, 2)),
        ("Blue Herons", (2, 1)),
    ]


def test_teams_are_optional() -> None:
    session = parse_sessions(to_json(session_payload())).get_session(UUID(SESSION_ID))

    assert session is not None
    assert session.teams == ()


@pytest.mark.parametrize("entered", ["FOX-7Q2K", "fox-7q2k", "  Fox-7Q2k \n", "\tFOX-7Q2K"])
def test_find_team_ignores_case_and_surrounding_spaces(entered: str) -> None:
    repository = parse_sessions(
        to_json(
            with_teams(team()),
            with_teams(team("Blue Herons", CODE_B), session_id=OTHER_SESSION_ID),
        )
    )

    found = repository.find_team(entered)

    assert found is not None
    session, matched = found
    assert (session.id, matched.name) == (UUID(SESSION_ID), "Red Foxes")


def test_find_team_finds_the_right_session() -> None:
    repository = parse_sessions(
        to_json(
            with_teams(team()),
            with_teams(team("Blue Herons", CODE_B), session_id=OTHER_SESSION_ID),
        )
    )

    found = repository.find_team(CODE_B.lower())

    assert found is not None
    assert found[0].id == UUID(OTHER_SESSION_ID)


@pytest.mark.parametrize("entered", ["FOX-7Q2", "FOX7Q2K", "", "FOX-7Q2K-X"])
def test_unknown_join_code_is_none(entered: str) -> None:
    repository = parse_sessions(to_json(with_teams(team())))

    assert repository.find_team(entered) is None


def test_get_team_by_name_ignoring_case() -> None:
    repository = parse_sessions(to_json(with_teams(team())))

    found = repository.get_team(UUID(SESSION_ID), "red foxes")

    assert found is not None
    assert found.name == "Red Foxes"
    assert repository.get_team(UUID(SESSION_ID), "Blue Herons") is None
    assert repository.get_team(UUID(OTHER_SESSION_ID), "Red Foxes") is None


def test_join_code_is_hidden_from_repr() -> None:
    repository = parse_sessions(to_json(with_teams(team())))
    session = repository.get_session(UUID(SESSION_ID))
    assert session is not None

    assert CODE_A not in repr(session.teams[0])
    assert CODE_A not in repr(session)


@pytest.mark.parametrize(
    ("teams", "line"),
    [
        pytest.param(
            [team(order=[1])], "[0].teams[0].order: misses checkpoint(s) 2", id="order-misses"
        ),
        pytest.param(
            [team(order=[1, 1, 2])], "[0].teams[0].order: repeats checkpoint 1", id="order-repeats"
        ),
        pytest.param(
            [team(order=[1, 2, 9])],
            "[0].teams[0].order: names unknown checkpoint 9",
            id="order-unknown",
        ),
        pytest.param(
            [team(), team("Blue Herons", CODE_B, [2, 3])],
            "[0].teams[1].order: names unknown checkpoint 3",
            id="second-team-order",
        ),
        pytest.param(
            [team(), team("RED FOXES", CODE_B)],
            "[0].teams[1].name: duplicate team name",
            id="name-duplicate-ignoring-case",
        ),
        pytest.param(
            [team(), team("Blue Herons", CODE_A.lower())],
            "[0].teams[1].join-code: duplicate join code",
            id="code-duplicate-in-session",
        ),
        pytest.param(
            [team(code="FOX12")],
            "[0].teams[0].join-code: String should have at least 6 characters",
            id="code-too-short",
        ),
        pytest.param(
            [team(code="F" * 33)],
            "[0].teams[0].join-code: String should have at most 32 characters",
            id="code-too-long",
        ),
        pytest.param(
            [team(code="FOX 7Q2K")],
            "[0].teams[0].join-code: String should match pattern",
            id="code-space",
        ),
        pytest.param(
            [team(code="FOX_7Q2K")],
            "[0].teams[0].join-code: String should match pattern",
            id="code-underscore",
        ),
        pytest.param(
            [{**team(), "colour": "red"}],
            "[0].teams[0].colour: Extra inputs are not permitted",
            id="unknown-field",
        ),
        pytest.param(
            [team(name="")],
            "[0].teams[0].name: String should have at least 1 character",
            id="name-empty",
        ),
        pytest.param(
            [team(name="N" * 41)],
            "[0].teams[0].name: String should have at most 40 characters",
            id="name-too-long",
        ),
    ],
)
def test_invalid_teams_stop_loading_without_echoing_values(
    tmp_path: Path, teams: list[dict[str, Any]], line: str
) -> None:
    sessions_file = tmp_path / "sessions.json"
    sessions_file.write_text(to_json(with_teams(*teams)))

    with pytest.raises(SessionsFileError) as excinfo:
        load_session_repository(sessions_file)

    message = str(excinfo.value)
    assert f"  {line}" in message
    for value in (t["join-code"] for t in teams):
        assert value not in message
        assert value.upper() not in message


def test_join_code_duplicated_across_sessions(tmp_path: Path) -> None:
    sessions_file = tmp_path / "sessions.json"
    sessions_file.write_text(
        to_json(
            with_teams(team()),
            with_teams(team("Blue Herons", "  " + CODE_A.lower()), session_id=OTHER_SESSION_ID),
        )
    )

    with pytest.raises(SessionsFileError) as excinfo:
        load_session_repository(sessions_file)

    message = str(excinfo.value)
    assert "  [1].teams[0].join-code: duplicate join code" in message
    assert CODE_A not in message.upper()


def test_every_duplicate_code_is_reported() -> None:
    with pytest.raises(SessionsFileError) as excinfo:
        parse_sessions(
            to_json(
                with_teams(team(), team("Blue Herons", CODE_B)),
                with_teams(team(code=CODE_B), team("Others", CODE_A), session_id=OTHER_SESSION_ID),
            )
        )

    message = str(excinfo.value)
    assert "2 validation error(s)" in message
    assert "[1].teams[0].join-code: duplicate join code" in message
    assert "[1].teams[1].join-code: duplicate join code" in message


def test_example_file_has_teams_with_different_orders() -> None:
    session = load_session_repository(EXAMPLE_FILE).get_session(UUID(SESSION_ID))

    assert session is not None
    assert len(session.teams) >= 2
    assert len({t.order for t in session.teams}) == len(session.teams)


def test_surrounding_spaces_in_the_file_are_trimmed() -> None:
    repository = parse_sessions(to_json(with_teams(team(code=f"  {CODE_A} "))))

    found = repository.find_team(CODE_A)

    assert found is not None
    assert found[1].join_code == CODE_A

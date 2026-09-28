import json
from uuid import UUID

import pytest
from fastapi import HTTPException

from game_server.lookup import find_checkpoint
from game_server.sessions import SessionRepository, parse_sessions

SESSION = UUID("aeffe667-4f9f-4108-b5e2-56ae821fe413")


@pytest.fixture
def sessions() -> SessionRepository:
    checkpoint = {
        "sequence": 1,
        "name": "Spot",
        "clue": "Find it",
        "location": {"lat": 51.5, "long": -0.1},
        "proximity": 40,
    }
    return parse_sessions(
        json.dumps(
            [
                {
                    "id": str(SESSION),
                    "name": "Hunt",
                    "location": "Here",
                    "start-time": "2026-10-03T10:00:00Z",
                    "end-time": "2026-10-03T12:00:00Z",
                    "checkpoints": [checkpoint],
                }
            ]
        )
    )


def test_finds_session_and_checkpoint(sessions: SessionRepository) -> None:
    session, checkpoint = find_checkpoint(sessions, SESSION, 1)

    assert (session.id, checkpoint.sequence) == (SESSION, 1)


@pytest.mark.parametrize(
    ("session_id", "sequence", "detail"),
    [(UUID(int=9), 1, "unknown session"), (SESSION, 2, "unknown checkpoint")],
)
def test_unknown_is_404(
    sessions: SessionRepository, session_id: UUID, sequence: int, detail: str
) -> None:
    with pytest.raises(HTTPException) as excinfo:
        find_checkpoint(sessions, session_id, sequence)

    assert (excinfo.value.status_code, excinfo.value.detail) == (404, detail)

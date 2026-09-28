"""Resolve a request's session and checkpoint, the same way for every endpoint."""

from uuid import UUID

from fastapi import HTTPException, status

from game_server.sessions import Checkpoint, GameSession, SessionRepository


def find_checkpoint(
    sessions: SessionRepository, session_id: UUID, sequence: int
) -> tuple[GameSession, Checkpoint]:
    """Return the session and its checkpoint, or fail with 404.

    `detail` is `unknown session` or `unknown checkpoint`, so clients can tell them apart.
    """
    session = sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session")
    checkpoint = sessions.get_checkpoint(session_id, sequence)
    if checkpoint is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown checkpoint")
    return session, checkpoint

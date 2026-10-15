"""Only a team's members can play as that team.

With accounts on, a request to a play route names the team's `participant` (in the path or the
body) and carries a Clerk token; the caller's `pid` must be one of the team's `team_members`.
Anyone else gets the same `404` as for a team that doesn't exist, so ids can't be probed.
`GAME_SERVER_AUTH_MODE` says when this applies: `off` never, `optional` when a token is sent,
`required` always (no token is a `401`).
"""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status

from game_server.auth import Principal, current_person, optional_person
from game_server.config import Settings, get_settings
from game_server.submissions import SubmissionStore


def play_person(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> Principal | None:
    """The caller of a play route: who they are, or None when no token is needed or sent.

    `required` demands a valid token; `optional` checks one when it's sent; `off` ignores it.
    """
    if settings.auth_mode == "required":
        return current_person(settings, authorization)
    return optional_person(settings, authorization)


def require_member(
    store: SubmissionStore, person: Principal | None, session: UUID, participant: UUID
) -> None:
    """404 `unknown participant` unless `person` is a member of the team; no person: no check.

    Called first in a handler, before the work (reading a photo, calling the referee).
    """
    if person is not None and not store.is_member(session, participant, person.pid):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown participant")

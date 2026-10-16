"""A person's own data rights: `DELETE /me` (erasure) and `GET /me/export`.

Both need a Clerk token and the users directory. The erasure itself is the job in `erasure`;
the export reads the directory through its repository and the game by `pid`. No photos.
"""

import logging
from datetime import UTC
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Body, Depends, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from game_server.auth import Principal, current_person, effective_role
from game_server.clock import Clock, get_clock
from game_server.config import Settings, get_settings
from game_server.database import Database, get_database
from game_server.directory.consents import UPCOMING_HUNTS_WORDINGS
from game_server.directory.repository import DirectoryRepository
from game_server.erasure import ErasureJob, GameRecords, get_erasure_job, run_now
from game_server.profile import directory
from game_server.sessions import SessionRepository, get_session_repository

logger = logging.getLogger(__name__)

router = APIRouter()

EXPORT_FILENAME = "scavenger-hunt-data.json"


class EraseIn(BaseModel):
    """An erasure's options: whether the person asks never to be contacted again."""

    model_config = ConfigDict(extra="forbid", validate_by_name=True)

    do_not_contact: bool = Field(default=False, alias="doNotContact")


class ErasureOut(BaseModel):
    """An erasure under way: the person's pid."""

    pid: UUID
    status: str = "erasing"


@router.delete(
    "/me",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        401: {"description": "No valid Clerk session token (code: unauthorised)"},
        503: {"description": "No directory (code: directory_unavailable)"},
    },
)
def erase_me(
    person: Annotated[Principal, Depends(current_person)],
    job: Annotated[ErasureJob, Depends(get_erasure_job)],
    background: BackgroundTasks,
    body: Annotated[EraseIn | None, Body()] = None,
) -> ErasureOut:
    """Erase the caller: everything the server holds about them, and their Clerk account.

    Answers at once: the erasure is recorded and their sessions are revoked, so this token
    stops working within a minute; the rest runs in the background, and is retried until done.
    """
    do_not_contact = body is not None and body.do_not_contact
    job.request(person.pid, "self", do_not_contact)
    background.add_task(run_now, job, person.pid)
    return ErasureOut(pid=person.pid)


def _camel(name: str) -> str:
    first, *rest = name.split("_")
    return first + "".join(part.title() for part in rest)


def _camel_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{_camel(key): value for key, value in row.items()} for row in rows]


@router.get(
    "/me/export",
    responses={
        200: {"description": "Everything held about the caller, as a JSON file. No photos."},
        401: {"description": "No valid Clerk session token (code: unauthorised)"},
        503: {"description": "No directory (code: directory_unavailable)"},
    },
)
def export_me(
    person: Annotated[Principal, Depends(current_person)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    people: Annotated[DirectoryRepository, Depends(directory)],
    database: Annotated[Database, Depends(get_database)],
    sessions: Annotated[SessionRepository, Depends(get_session_repository)],
) -> JSONResponse:
    """The caller's profile, consents, role and play history, by their pid."""
    pid = person.pid
    profile = people.profile(pid)
    account = people.account(pid)
    history = GameRecords(database).history(pid)
    teams = [
        {**team, "hunt": found.name if (found := sessions.get_session(team["session"])) else None}
        for team in history.teams
    ]
    export = {
        "exportedAt": clock().astimezone(UTC),
        "pid": pid,
        "account": None
        if account is None
        else {"createdAt": account.created_at, "lastSeenAt": account.last_seen_at},
        "profile": None
        if profile is None
        else {
            "email": profile.email,
            "displayName": profile.display_name,
            "homeArea": profile.home_area,
            "locale": profile.locale,
            "updatedAt": profile.updated_at,
        },
        "role": effective_role(person, settings),
        "consents": [
            {
                "purpose": consent.purpose,
                "grantedAt": consent.granted_at,
                "withdrawnAt": consent.withdrawn_at,
                "wordingVersion": consent.wording_version,
                "wording": UPCOMING_HUNTS_WORDINGS.get(consent.wording_version),
                "source": consent.source,
            }
            for consent in people.consent_history(pid)
        ],
        "teams": _camel_rows(teams),
        "photos": _camel_rows(history.photos),
        "checkIns": _camel_rows(history.check_ins),
        "rulings": _camel_rows(history.rulings),
        "moderating": _camel_rows(history.moderating),
        "drafts": _camel_rows(history.drafts),
    }
    logger.info("Data exported for person %s", pid)
    return JSONResponse(
        jsonable_encoder(export),
        headers={"Content-Disposition": f'attachment; filename="{EXPORT_FILENAME}"'},
    )

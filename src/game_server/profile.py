"""The signed-in player's profile: `/me`, the upcoming-hunts consent, their hunts and teams.

All routes need a Clerk token. Profile reads and writes go through the users directory's own
repository; the game's tables are read only for the hunts. Logs name a person by `pid` only.
"""

import logging
import re
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from game_server.auth import Principal, current_person, effective_role
from game_server.clock import Clock, get_clock
from game_server.config import Settings, get_settings
from game_server.directory.consents import UPCOMING_HUNTS_WORDINGS
from game_server.directory.repository import (
    Consent,
    DirectoryNotConfiguredError,
    DirectoryRepository,
    Profile,
    get_directory,
)
from game_server.errors import ApiError
from game_server.join import find_participant
from game_server.membership import require_member
from game_server.scoring import team_points
from game_server.sessions import SessionRepository, get_session_repository
from game_server.submissions import SubmissionStore, get_submission_store

logger = logging.getLogger(__name__)

router = APIRouter()

SUPPORTED_LOCALES = frozenset({"en-GB"})
DISPLAY_NAME_MAX = 40
# A UK postcode district: W4, SW1A, EC1A. Never a full postcode.
HOME_AREA = re.compile(r"^[A-Z]{1,2}[0-9][0-9A-Z]?$")


def directory(settings: Annotated[Settings, Depends(get_settings)]) -> DirectoryRepository:
    """The users directory, or `503 directory_unavailable` when there isn't one."""
    try:
        return get_directory(settings)
    except DirectoryNotConfiguredError:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "the users directory isn't set up",
            "directory_unavailable",
        ) from None


def _camel(name: str) -> str:
    first, *rest = name.split("_")
    return first + "".join(part.title() for part in rest)


class _Out(BaseModel):
    """A response body: camelCase keys, as the web app reads them."""

    model_config = ConfigDict(
        alias_generator=_camel, validate_by_name=True, serialize_by_alias=True
    )


class ConsentOut(_Out):
    """Whether the upcoming-hunts consent is in force, and since when."""

    granted: bool
    since: datetime | None
    wording_version: str | None


class MeOut(_Out):
    """The signed-in person: never their email."""

    pid: UUID
    display_name: str | None
    home_area: str | None
    locale: str
    role: str
    upcoming_hunts: ConsentOut


def consent_out(consent: Consent) -> ConsentOut:
    """The consent as the API shows it."""
    return ConsentOut(
        granted=consent.granted, since=consent.since, wording_version=consent.wording_version
    )


def me_out(profile: Profile, role: str, consent: Consent) -> MeOut:
    """The person's profile, role and consent as one response."""
    return MeOut(
        pid=profile.pid,
        display_name=profile.display_name,
        home_area=profile.home_area,
        locale=profile.locale,
        role=role,
        upcoming_hunts=consent_out(consent),
    )


def read_me(person: Principal, settings: Settings, people: DirectoryRepository) -> MeOut:
    """The caller's `MeOut`; `404 unknown_person` if the directory has no profile for them."""
    profile = people.profile(person.pid)
    if profile is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "unknown person", "unknown_person")
    return me_out(
        profile, effective_role(person, settings), people.upcoming_hunts_consent(person.pid)
    )


@router.get(
    "/me",
    responses={
        401: {"description": "No valid Clerk session token (code: unauthorised)"},
        404: {"description": "The directory has no profile for the caller (code: unknown_person)"},
        503: {"description": "No directory (code: directory_unavailable)"},
    },
)
def get_me(
    person: Annotated[Principal, Depends(current_person)],
    settings: Annotated[Settings, Depends(get_settings)],
    people: Annotated[DirectoryRepository, Depends(directory)],
) -> MeOut:
    """The caller's profile, role and upcoming-hunts consent."""
    return read_me(person, settings, people)


class ProfileChange(BaseModel):
    """Body of `PATCH /me`: the fields to change. Each is checked by `profile_changes`, so a
    bad one gets a `code` of its own; a field that isn't sent stays as it is."""

    model_config = ConfigDict(extra="forbid", validate_by_name=True)

    display_name: Any = Field(default=None, alias="displayName")
    home_area: Any = Field(default=None, alias="homeArea")
    locale: Any = None


def _text(value: object) -> str | None:
    """A string trimmed, `""` as None (clear); anything but a string or null is not valid."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError
    return value.strip() or None


def profile_changes(body: ProfileChange) -> dict[str, str | None]:
    """The profile fields `body` sets, normalised; `422 invalid_profile` naming a code per bad
    field, and nothing changes. Values are never echoed back."""
    changes: dict[str, str | None] = {}
    problems: dict[str, str] = {}
    sent = body.model_fields_set
    if "display_name" in sent:
        try:
            name = _text(body.display_name)
            if name is not None and len(name) > DISPLAY_NAME_MAX:
                raise ValueError
            changes["display_name"] = name
        except ValueError:
            problems["displayName"] = "invalid_display_name"
    if "home_area" in sent:
        try:
            area = _text(body.home_area)
            if area is not None and not HOME_AREA.match(area.upper()):
                raise ValueError
            changes["home_area"] = area.upper() if area else None
        except ValueError:
            problems["homeArea"] = "invalid_home_area"
    if "locale" in sent:
        if body.locale in SUPPORTED_LOCALES:
            changes["locale"] = body.locale
        else:
            problems["locale"] = "unsupported_locale"
    if problems:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid profile",
            "invalid_profile",
            extra={"fields": problems},
        )
    return changes


@router.patch(
    "/me",
    responses={
        401: {"description": "No valid Clerk session token (code: unauthorised)"},
        404: {"description": "The caller isn't an active person (code: unknown_person)"},
        422: {"description": "A field is invalid (code: invalid_profile, with a code per field)"},
        503: {"description": "No directory (code: directory_unavailable)"},
    },
)
def patch_me(
    body: ProfileChange,
    person: Annotated[Principal, Depends(current_person)],
    settings: Annotated[Settings, Depends(get_settings)],
    people: Annotated[DirectoryRepository, Depends(directory)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> MeOut:
    """Change the caller's display name, home area or locale; all or nothing."""
    changes = profile_changes(body)
    if not people.update_profile(person.pid, changes, clock().astimezone(UTC)):
        raise ApiError(status.HTTP_404_NOT_FOUND, "unknown person", "unknown_person")
    logger.info("Profile changed person %s fields %s", person.pid, sorted(changes))
    return read_me(person, settings, people)


class ConsentChange(BaseModel):
    """Body of `PUT /me/consents/upcoming-hunts`."""

    model_config = ConfigDict(extra="forbid", validate_by_name=True)

    granted: bool
    wording_version: str | None = Field(default=None, alias="wordingVersion")


@router.put(
    "/me/consents/upcoming-hunts",
    responses={
        401: {"description": "No valid Clerk session token (code: unauthorised)"},
        404: {"description": "The caller isn't an active person (code: unknown_person)"},
        422: {"description": "A grant without a known wording version (code: unknown_wording)"},
        503: {"description": "No directory (code: directory_unavailable)"},
    },
)
def put_upcoming_hunts_consent(
    body: ConsentChange,
    person: Annotated[Principal, Depends(current_person)],
    people: Annotated[DirectoryRepository, Depends(directory)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> ConsentOut:
    """Grant the consent under a wording version (a new row), or withdraw the one in force
    (its row is kept, with `withdrawn_at`). Repeating either changes nothing."""
    now = clock().astimezone(UTC)
    if body.granted:
        if body.wording_version not in UPCOMING_HUNTS_WORDINGS:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "a known wordingVersion is required to grant",
                "unknown_wording",
            )
        people.grant_upcoming_hunts(person.pid, body.wording_version, "profile", now)
    else:
        people.withdraw_upcoming_hunts(person.pid, now)
    consent = people.upcoming_hunts_consent(person.pid)
    logger.info("Upcoming hunts consent person %s granted %s", person.pid, consent.granted)
    return consent_out(consent)


class CheckpointsOut(_Out):
    """How many of a team's checkpoints it has completed, out of how many."""

    completed: int
    total: int


class HuntOut(_Out):
    """One hunt the player took part in."""

    session: UUID
    participant: UUID
    name: str
    team: str
    date: datetime
    checkpoints: CheckpointsOut
    points: int


class HuntsOut(_Out):
    """The player's hunts, newest first."""

    hunts: list[HuntOut]


@router.get(
    "/me/hunts",
    responses={401: {"description": "No valid Clerk session token (code: unauthorised)"}},
)
def my_hunts(
    person: Annotated[Principal, Depends(current_person)],
    sessions: Annotated[SessionRepository, Depends(get_session_repository)],
    store: Annotated[SubmissionStore, Depends(get_submission_store)],
) -> HuntsOut:
    """The hunts the caller joined as a member, newest first, with how their team did.

    `points` is what the team would score if the session finished now (lower is better). A hunt
    whose session or team is no longer in the sessions file is left out.
    """
    hunts = []
    for membership in store.memberships(person.pid):
        session = sessions.get_session(membership.session)
        team = sessions.get_team(membership.session, membership.team)
        if session is None or team is None:
            continue
        completed = store.completed_checkpoints(membership.session, membership.participant)
        results = store.session_results(membership.session)
        hunts.append(
            HuntOut(
                session=session.id,
                participant=membership.participant,
                name=session.name,
                team=team.name,
                date=session.start_time,
                checkpoints=CheckpointsOut(
                    completed=sum(1 for sequence in team.order if sequence in completed),
                    total=len(team.order),
                ),
                points=team_points(team, results).points,
            )
        )
    return HuntsOut(hunts=hunts)


class MemberOut(_Out):
    """A team member: their display name, if they set one. Never an email or a pid."""

    display_name: str | None
    you: bool


class MembersOut(_Out):
    """A team's members, in the order they joined."""

    members: list[MemberOut]


@router.get(
    "/sessions/{session}/participants/{participant}/members",
    responses={
        401: {"description": "No valid Clerk session token (code: unauthorised)"},
        404: {"description": "Unknown session, or a team the caller isn't a member of"},
        503: {"description": "No directory (code: directory_unavailable)"},
    },
)
def team_members(
    session: UUID,
    participant: UUID,
    person: Annotated[Principal, Depends(current_person)],
    sessions: Annotated[SessionRepository, Depends(get_session_repository)],
    store: Annotated[SubmissionStore, Depends(get_submission_store)],
    people: Annotated[DirectoryRepository, Depends(directory)],
) -> MembersOut:
    """The display names of the team's members, for the team only."""
    if sessions.get_session(session) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session")
    require_member(store, person, session, participant)
    if find_participant(store, sessions, session, participant) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown participant")
    pids = store.team_members(session, participant)
    names = people.display_names(pids)
    return MembersOut(
        members=[MemberOut(display_name=names.get(pid), you=pid == person.pid) for pid in pids]
    )

"""`POST /checkpoint/proximity`: an advisory in-range hint for the app. Never a check.

The app may warn a player who looks out of range before they submit. It can't work that
out itself, because the checkpoint's coordinates are the answer to the clue. So the server
answers yes or no, and nothing else. The hint records nothing and plays no part in any
verdict: the geofence verdict is always decided by `POST /challenge`.
"""

from datetime import UTC, timedelta
from functools import lru_cache
from math import ceil
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from game_server.checks.geofence import within_proximity
from game_server.checks.time_window import window_is_open
from game_server.clock import Clock, get_clock
from game_server.config import Settings, get_settings
from game_server.geo import distance_m
from game_server.lookup import find_checkpoint
from game_server.models import ProximityHint, ProximityHintRequest
from game_server.rate_limit import RateLimiter
from game_server.sessions import SessionRepository, get_session_repository

router = APIRouter()


@lru_cache
def hint_rate_limiter(interval_seconds: int) -> RateLimiter:
    """The process-wide limiter for a given interval."""
    return RateLimiter(timedelta(seconds=interval_seconds))


def get_hint_rate_limiter(settings: Annotated[Settings, Depends(get_settings)]) -> RateLimiter:
    """Dependency: one request per (session, participant) per configured interval."""
    return hint_rate_limiter(settings.proximity_hint_interval_seconds)


@router.post(
    "/checkpoint/proximity",
    responses={
        404: {"description": "Unknown session, or unknown checkpoint in the session"},
        429: {"description": "Asked again too soon; see the Retry-After header"},
    },
)
def proximity_hint(
    body: ProximityHintRequest,
    clock: Annotated[Clock, Depends(get_clock)],
    sessions: Annotated[SessionRepository, Depends(get_session_repository)],
    limiter: Annotated[RateLimiter, Depends(get_hint_rate_limiter)],
) -> ProximityHint:
    """Whether the player looks in range of an open checkpoint. Advisory only.

    `false` if the location is outside the checkpoint's area *or* the checkpoint isn't
    open, without saying which. It's a POST so the location stays out of URLs and access
    logs; nothing is stored and the coordinates are not logged.
    """
    now = clock().astimezone(UTC)
    session, checkpoint = find_checkpoint(sessions, body.session, body.checkpoint)
    wait = limiter.check((body.session, body.participant), now)
    if wait is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too many proximity checks, try again shortly",
            headers={"Retry-After": str(ceil(wait.total_seconds()))},
        )
    in_range = window_is_open(session, checkpoint, now) and within_proximity(
        distance_m(body.location, checkpoint.location), checkpoint
    )
    return ProximityHint(in_range=in_range)

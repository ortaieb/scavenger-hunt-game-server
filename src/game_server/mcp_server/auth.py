"""The organiser key in front of `/mcp`, as an ASGI guard.

The same key, check and `401` as the hunt designer's (`organiser`). Through MCP, the key covers
every session's moderator actions, so moderator codes never pass through Claude. Failed
attempts are rate-limited per client. A stopgap until OAuth (#131).
"""

from datetime import timedelta
from math import ceil

from fastapi import status
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from game_server.clock import Clock
from game_server.config import Settings
from game_server.organiser import UNAUTHORISED_BODY, organiser_key_matches
from game_server.rate_limit import RateLimiter

# After a failed attempt, the client waits this long before it may try again (any key).
FAILED_ATTEMPT_INTERVAL = timedelta(seconds=1)
RATE_LIMITED_BODY = {
    "detail": "too many failed attempts, try again shortly",
    "code": "organiser_rate_limited",
}


class OrganiserGuard:
    """Passes a request to `app` only if it presents the organiser key.

    Else `401` with the designer's body and `WWW-Authenticate: Bearer`, whatever the reason.
    A client that failed less than `FAILED_ATTEMPT_INTERVAL` ago gets `429` with `Retry-After`,
    before its key is looked at, so guesses can't go faster than one per interval.
    """

    def __init__(self, app: ASGIApp, settings: Settings, clock: Clock) -> None:
        self._app = app
        self._settings = settings
        self._clock = clock
        self._failures = RateLimiter(FAILED_ATTEMPT_INTERVAL)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Check the key, then hand over."""
        client = scope.get("client")
        key = client[0] if client else None
        now = self._clock()
        wait = self._failures.waiting(key, now)
        if wait is not None:
            response = JSONResponse(
                RATE_LIMITED_BODY,
                status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"Retry-After": str(ceil(wait.total_seconds()))},
            )
        elif organiser_key_matches(self._settings, Headers(scope=scope).get("authorization")):
            await self._app(scope, receive, send)
            return
        else:
            self._failures.check(key, now)
            response = JSONResponse(
                UNAUTHORISED_BODY,
                status.HTTP_401_UNAUTHORIZED,
                headers={"WWW-Authenticate": "Bearer"},
            )
        await response(scope, receive, send)

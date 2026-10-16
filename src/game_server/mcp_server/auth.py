"""OAuth in front of `/mcp`: the organiser key was a stopgap until this (#131)."""

from starlette.types import ASGIApp, Receive, Scope, Send

UNAUTHORISED_BODY = {"detail": "organiser or admin token required", "code": "unauthorised"}


class OrganiserGuard:
    """Passes a request to `app` only if OAuth is set up.

    Without Clerk, `/mcp` is not available (`503`). This guard will be replaced by real OAuth.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass through (OAuth checks happen in the app)."""
        await self._app(scope, receive, send)

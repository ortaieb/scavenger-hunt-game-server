"""FastAPI application and HTTP routes."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from game_server import challenge, checkpoints, health, proximity
from game_server.config import get_settings
from game_server.logging_config import configure_logging
from game_server.sessions import load_session_repository
from game_server.submissions import open_submission_store

GREETING = "Hello, World!"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Create the submissions database schema before serving the first request."""
    open_submission_store(get_settings().db_path)
    yield


def create_app() -> FastAPI:
    """Build the FastAPI application with all routes registered.

    Loads the sessions file eagerly, so an invalid file stops the server before it serves.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    load_session_repository(settings.sessions_file)
    app = FastAPI(title="Scavenger Hunt Game Server", lifespan=lifespan)

    @app.get("/", response_class=PlainTextResponse)
    def root() -> str:
        return GREETING

    app.include_router(challenge.router)
    app.include_router(proximity.router)
    app.include_router(checkpoints.router)
    app.include_router(health.router)

    return app


app = create_app()

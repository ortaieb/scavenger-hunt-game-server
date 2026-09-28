"""FastAPI application and HTTP routes."""

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from game_server import challenge
from game_server.config import get_settings
from game_server.logging_config import configure_logging
from game_server.sessions import load_session_repository

GREETING = "Hello, World!"


def create_app() -> FastAPI:
    """Build the FastAPI application with all routes registered.

    Loads the sessions file eagerly, so an invalid file stops the server before it serves.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    load_session_repository(settings.sessions_file)
    app = FastAPI(title="Scavenger Hunt Game Server")

    @app.get("/", response_class=PlainTextResponse)
    def root() -> str:
        return GREETING

    app.include_router(challenge.router)

    return app


app = create_app()

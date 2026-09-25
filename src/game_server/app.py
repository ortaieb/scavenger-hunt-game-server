"""FastAPI application and HTTP routes."""

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from game_server import challenge
from game_server.config import get_settings
from game_server.logging_config import configure_logging

GREETING = "Hello, World!"


def create_app() -> FastAPI:
    """Build the FastAPI application with all routes registered."""
    configure_logging(get_settings().log_level)
    app = FastAPI(title="Scavenger Hunt Game Server")

    @app.get("/", response_class=PlainTextResponse)
    def root() -> str:
        return GREETING

    app.include_router(challenge.router)

    return app


app = create_app()

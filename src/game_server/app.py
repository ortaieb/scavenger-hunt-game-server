"""FastAPI application and HTTP routes."""

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

GREETING = "Hello, World!"


def create_app() -> FastAPI:
    """Build the FastAPI application with all routes registered."""
    app = FastAPI(title="Scavenger Hunt Game Server")

    @app.get("/", response_class=PlainTextResponse)
    def root() -> str:
        return GREETING

    return app


app = create_app()

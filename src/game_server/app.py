"""FastAPI application and HTTP routes."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse

from game_server import (
    arrive,
    challenge,
    checkpoints,
    game_state,
    health,
    join,
    moderation,
    proximity,
)
from game_server.config import get_settings
from game_server.database import close_databases, database_config, open_database
from game_server.logging_config import configure_logging
from game_server.sessions import load_session_repository

GREETING = "Hello, World!"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Open the database's connection pool before serving, and close it on shutdown.

    The pool connects in the background, so an unreachable database doesn't stop startup:
    `/health` reports it unavailable until it answers.
    """
    open_database(database_config(get_settings()))
    try:
        yield
    finally:
        close_databases()


async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """A 422 like FastAPI's default, minus each error's `input`.

    The default echoes the submitted values, for a missing field the whole body, which would
    return a join code (a credential) or a player's coordinates in the response.
    """
    errors = [
        {key: value for key, value in error.items() if key != "input"} for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


def create_app() -> FastAPI:
    """Build the FastAPI application with all routes registered.

    Loads the sessions file eagerly, so an invalid file stops the server before it serves.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    load_session_repository(settings.sessions_file, settings.max_image_bytes)
    app = FastAPI(title="Scavenger Hunt Game Server", lifespan=lifespan)
    app.add_exception_handler(RequestValidationError, validation_error)  # type: ignore[arg-type]  # Starlette types handlers on the base Exception
    moderation.install(app)

    @app.get("/", response_class=PlainTextResponse)
    def root() -> str:
        return GREETING

    app.include_router(challenge.router)
    app.include_router(proximity.router)
    app.include_router(checkpoints.router)
    app.include_router(health.router)
    app.include_router(join.router)
    app.include_router(game_state.router)
    app.include_router(arrive.router)

    return app


app = create_app()

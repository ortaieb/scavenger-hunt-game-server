"""FastAPI application and HTTP routes."""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse

from game_server import (
    arrive,
    challenge,
    game_state,
    health,
    join,
    moderation,
    moderator_routes,
    overview,
    photos,
    proximity,
    review,
    ruling,
    session_control,
    traces,
)
from game_server.clock import get_clock
from game_server.config import get_settings
from game_server.database import close_databases, database_config, open_database
from game_server.designer import routes as designer_routes
from game_server.designer_runners import AgentRunner, interrupt_left_running
from game_server.directory import routes as directory_routes
from game_server.directory.sync import start_clerk_sync
from game_server.drafts import DraftStore
from game_server.logging_config import configure_logging
from game_server.mcp_server.server import mount_mcp
from game_server.sessions import load_session_repository

GREETING = "Hello, World!"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the database's connection pool and the designer's agent runner before serving;
    on shutdown, stop any design run, then close the pool. With the directory and a Clerk
    key, the outbox's writes to Clerk are sent in the background.

    The pool connects in the background, so an unreachable database doesn't stop startup:
    `/health` reports it unavailable until it answers. Drafts a previous process left
    `running` are failed with `interrupted` first. The MCP server's session manager, if
    mounted, runs for as long as the app.
    """
    settings = get_settings()
    store = DraftStore(open_database(database_config(settings)))
    await interrupt_left_running(store, get_clock())
    runner = AgentRunner(store, get_clock(), settings)
    await runner.open()
    app.state.agent_runner = runner
    clerk_sync = start_clerk_sync(settings, get_clock())
    manager = getattr(app.state, "mcp_session_manager", None)
    mcp: AbstractAsyncContextManager[None] = manager.run() if manager else nullcontext()
    try:
        async with mcp:
            yield
    finally:
        if clerk_sync is not None:
            await clerk_sync.stop()
        await runner.close()
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
    app.include_router(health.router)
    app.include_router(join.router)
    app.include_router(game_state.router)
    app.include_router(arrive.router)
    app.include_router(session_control.router)
    app.include_router(overview.router)
    app.include_router(traces.router)
    app.include_router(designer_routes.router)
    designer_routes.install(app)
    app.include_router(ruling.router)
    app.include_router(review.router)
    app.include_router(photos.router)
    app.include_router(directory_routes.router)
    app.include_router(moderator_routes.router)
    if settings.mcp_enabled:
        mount_mcp(app, settings)

    return app


app = create_app()

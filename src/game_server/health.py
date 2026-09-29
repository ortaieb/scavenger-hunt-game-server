"""`GET /health`: the readiness signal the platform uses before switching traffic."""

import logging
import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict

from game_server.config import Settings, get_settings
from game_server.submissions import open_submission_store

logger = logging.getLogger(__name__)

router = APIRouter()


class HealthStatus(BaseModel):
    """Only a status: nothing about the deployment's internals."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "unavailable"]


@router.get(
    "/health",
    responses={503: {"model": HealthStatus, "description": "The database isn't reachable"}},
)
def health(
    response: Response, settings: Annotated[Settings, Depends(get_settings)]
) -> HealthStatus:
    """Ready when the submissions database answers.

    Checking the database (not just that the process is up) means a deploy whose volume is
    missing or not writable stays unhealthy, and the platform keeps traffic on the old one.
    Sessions are already validated at startup, so a bad sessions file never gets this far.
    """
    try:
        open_submission_store(settings.db_path).ping()
    except (sqlite3.Error, OSError) as exc:
        logger.warning("health check failed: database unavailable (%s)", type(exc).__name__)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthStatus(status="unavailable")
    return HealthStatus(status="ok")

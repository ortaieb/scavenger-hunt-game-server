"""`GET /health`: the readiness signal the platform uses before switching traffic."""

import logging
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict

from game_server.submissions import SubmissionStore, get_submission_store

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
    response: Response, store: Annotated[SubmissionStore, Depends(get_submission_store)]
) -> HealthStatus:
    """Ready when the database answers and has its schema.

    Checking the database (not just that the process is up) means a deploy that can't reach
    it, or that isn't allowed in, stays unhealthy, and the platform keeps traffic on the old
    one. Sessions are already validated at startup, so a bad sessions file never gets this far.
    """
    try:
        store.ping()
    except psycopg.Error as exc:
        logger.warning("health check failed: database unavailable (%s)", type(exc).__name__)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthStatus(status="unavailable")
    return HealthStatus(status="ok")

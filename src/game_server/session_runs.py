"""A session's run: when the moderator started and finished it.

The sessions file's start-time and end-time are only the planned window, used for display.
The run decides whether a session is on.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

SessionPhase = Literal["scheduled", "running", "stopped"]


@dataclass(frozen=True)
class SessionRun:
    """When the moderator started and stopped the session, if they have."""

    started_at: datetime | None
    stopped_at: datetime | None


def session_phase(run: SessionRun | None) -> SessionPhase:
    """`scheduled` until started, `running` until stopped, then `stopped` (final).

    The planned times play no part.
    """
    if run is None or run.started_at is None:
        return "scheduled"
    if run.stopped_at is None:
        return "running"
    return "stopped"


@dataclass(frozen=True)
class RunChange:
    """The session's run after a start or stop, and whether that call changed it."""

    run: SessionRun | None
    changed: bool

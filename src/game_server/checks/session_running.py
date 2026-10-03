"""The session must be running: photos outside it are recorded but don't count.

Photos before the moderator starts the session, or after they stop it, are still stored (so
teams can challenge results later), recorded as failed with a session rejection.
"""

from dataclasses import dataclass

from game_server.checks.base import CheckResult, Rejection, SubmissionContext
from game_server.session_runs import session_phase

SESSION_RUNNING = "session_running"
SESSION_NOT_STARTED = Rejection("session_not_started", "The session hasn't started yet.")
SESSION_STOPPED = Rejection(
    "session_stopped", "The session is over. This photo was recorded but doesn't count."
)


@dataclass(frozen=True)
class SessionRunningCheck:
    """Passes while the session is running; fails before a start and after a stop."""

    def __call__(self, ctx: SubmissionContext, /) -> CheckResult:
        """Decided by the session's run, never by the file's planned times."""
        phase = session_phase(ctx.run)
        if phase == "scheduled":
            return CheckResult.failed(SESSION_RUNNING, SESSION_NOT_STARTED)
        if phase == "stopped":
            return CheckResult.failed(SESSION_RUNNING, SESSION_STOPPED)
        return CheckResult.passed(SESSION_RUNNING, "The session is running.")

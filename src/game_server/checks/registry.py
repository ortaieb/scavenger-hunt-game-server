"""Which checks every submission goes through."""

from collections.abc import Sequence
from typing import Annotated

from fastapi import Depends

from game_server.checks.base import Check
from game_server.checks.checked_in import CheckedInCheck
from game_server.checks.duplicate_photo import DuplicatePhotoCheck
from game_server.checks.geofence import GeofenceCheck
from game_server.checks.session_running import SessionRunningCheck
from game_server.checks.time_window import TimeWindowCheck
from game_server.checks.visual import PoseCorrectCheck, SceneMatchesCheck
from game_server.config import Settings, get_settings


def get_checks(settings: Annotated[Settings, Depends(get_settings)]) -> Sequence[Check]:
    """Dependency providing the registered checks, configured from settings.

    Checks without the `InTransaction` marker come first: they run before the referee,
    outside the write lock. Each check's issue adds its check here.
    """
    return (
        SessionRunningCheck(),
        CheckedInCheck(),
        *TimeWindowCheck.from_settings(settings).rules(),
        GeofenceCheck(),
        DuplicatePhotoCheck(max_distance=settings.phash_max_distance),
        SceneMatchesCheck(min_confidence=settings.referee_min_confidence),
        PoseCorrectCheck(min_confidence=settings.referee_min_confidence),
    )

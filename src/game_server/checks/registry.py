"""Which checks every submission goes through."""

from collections.abc import Sequence
from typing import Annotated

from fastapi import Depends

from game_server.checks.base import Check
from game_server.checks.geofence import GeofenceCheck
from game_server.checks.time_window import TimeWindowCheck
from game_server.config import Settings, get_settings


def get_checks(settings: Annotated[Settings, Depends(get_settings)]) -> Sequence[Check]:
    """Dependency providing the registered checks, configured from settings.

    Each check's issue adds its check here.
    """
    return (*TimeWindowCheck.from_settings(settings).rules(), GeofenceCheck())

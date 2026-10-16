"""The wording of each consent, kept as agreed.

A consent row records the version of the wording the person saw, so what they agreed to can
always be shown again. Add a new version when the text changes; never edit or remove one that
has been offered.
"""

import logging
from datetime import datetime
from uuid import UUID

from game_server.directory.repository import DirectoryRepository

logger = logging.getLogger(__name__)

# Marketing email to individuals needs their specific consent (PECR).
UPCOMING_HUNTS_WORDINGS: dict[str, str] = {
    "2026-10-16": (
        "Email me about upcoming scavenger hunts. I can withdraw this at any time in my profile."
    ),
}

# What the join screen and the profile currently show.
CURRENT_UPCOMING_HUNTS_WORDING = "2026-10-16"


def record_join_consent(people: DirectoryRepository | None, pid: UUID, now: datetime) -> None:
    """Record the join screen's ticked box as a grant under the current wording.

    Without a directory nothing is recorded: no consent is assumed, and the player can give it
    in their profile.
    """
    if people is None:
        logger.warning("Upcoming hunts box ticked at join but no directory person %s", pid)
        return
    people.grant_upcoming_hunts(pid, CURRENT_UPCOMING_HUNTS_WORDING, "join", now)

"""A checkpoint's reference photos, prepared for the referee once and then kept in memory.

The sessions file lists the moderator's own photos of each checkpoint, and loading it checks
that they exist and decode. The referee sends the first `max_references` of them with every
photo judged at that checkpoint, so they're prepared like a player's photo (upright, EXIF
stripped, re-encoded) but smaller, and only once. Paths are never logged or raised: a file
name can describe the place.
"""

import logging
import threading
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import Depends

from game_server.config import Settings, get_settings
from game_server.imaging import UndecodableImageError
from game_server.referee import PreparedReference, prepare_image
from game_server.sessions import SessionRepository, get_session_repository

logger = logging.getLogger(__name__)


class ReferencePhotoError(Exception):
    """A reference photo can't be read or decoded. Names it by position, never by path."""


def prepare_references(
    paths: Sequence[Path], max_references: int, max_edge: int
) -> tuple[PreparedReference, ...]:
    """The first `max_references` photos, prepared with their long edge at most `max_edge`.

    The others aren't read. Raises `ReferencePhotoError` for the first one that can't be
    read or decoded.
    """
    prepared = []
    for position, path in enumerate(paths[:max_references]):
        try:
            data = path.read_bytes()
        except OSError as exc:  # its message holds the path: only the type is kept
            raise ReferencePhotoError(
                f"reference-photos[{position}]: can't be read ({type(exc).__name__})"
            ) from None
        try:
            image = prepare_image(data, max_edge)
        except UndecodableImageError:
            raise ReferencePhotoError(f"reference-photos[{position}]: doesn't decode") from None
        prepared.append(PreparedReference(position, image))
    return tuple(prepared)


class ReferencePhotos:
    """Each checkpoint's reference photos as the referee sends them, prepared on first use."""

    def __init__(self, sessions: SessionRepository, max_references: int, max_edge: int) -> None:
        self._sessions = sessions
        self.max_references = max_references
        self.max_edge = max_edge
        self._prepared: dict[tuple[UUID, int], tuple[PreparedReference, ...]] = {}
        self._lock = threading.Lock()

    def for_checkpoint(self, session: UUID, sequence: int) -> tuple[PreparedReference, ...]:
        """What to send with a photo at this checkpoint: () if it has none, or they're broken.

        Prepared the first time, then kept: a later call never reads the files again, even
        after a failure (logged once, by position).
        """
        key = (session, sequence)
        prepared = self._prepared.get(key)
        if prepared is not None:
            return prepared
        with self._lock:  # one thread prepares; the others wait, then find it ready
            prepared = self._prepared.get(key)
            if prepared is None:
                prepared = self._prepared[key] = self._prepare(session, sequence)
        return prepared

    def _prepare(self, session: UUID, sequence: int) -> tuple[PreparedReference, ...]:
        paths = self._sessions.reference_photos(session, sequence)
        try:
            return prepare_references(paths, self.max_references, self.max_edge)
        except ReferencePhotoError as exc:
            logger.warning(
                "referee: session %s checkpoint %s %s; sending no reference photos",
                session,
                sequence,
                exc,
            )
            return ()


@lru_cache
def build_reference_photos(
    sessions: SessionRepository, max_references: int, max_edge: int
) -> ReferencePhotos:
    """The process-wide reference photos for these sessions and settings."""
    return ReferencePhotos(sessions, max_references, max_edge)


def get_reference_photos(
    settings: Annotated[Settings, Depends(get_settings)],
    sessions: Annotated[SessionRepository, Depends(get_session_repository)],
) -> ReferencePhotos:
    """Dependency providing the reference photos for the configured sessions."""
    return build_reference_photos(
        sessions, settings.referee_max_references, settings.referee_reference_max_edge
    )

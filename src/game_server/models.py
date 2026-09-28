"""Request payload models."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

# `pass` is reserved: nothing can award it until presence-proof and visual checks exist.
VerdictStatus = Literal["failed", "pending", "pass"]


class Location(BaseModel):
    """WGS84 coordinates in decimal degrees."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    lat: float = Field(ge=-90, le=90)
    long: float = Field(ge=-180, le=180)


class ChallengeMetadata(BaseModel):
    """The `metadata` part of a challenge submission."""

    model_config = ConfigDict(extra="forbid", validate_by_name=True, validate_by_alias=True)

    session: UUID
    participant: UUID
    # Strict: "2", 2.0 and true are rejected rather than coerced to an int.
    checkpoint: int = Field(ge=1, strict=True, description="The checkpoint's `sequence`")
    location: Location
    capture_time: AwareDatetime = Field(alias="capture-time")


class RejectionOut(BaseModel):
    """A reason the submission failed, safe to show the player."""

    code: str
    message: str


class CheckpointVerdict(BaseModel):
    """The server's verdict on one attempt at one checkpoint."""

    sequence: int
    attempt: int
    time: datetime = Field(description="When the server received the submission (UTC)")
    verdict: VerdictStatus
    rejections: list[RejectionOut]


class Verdict(BaseModel):
    """Who submitted for which game, and the checkpoint verdict."""

    game: UUID
    participant: UUID
    checkpoint: CheckpointVerdict


class ChallengeVerdict(BaseModel):
    """Response to a received challenge."""

    verdict: Verdict
    image_id: UUID

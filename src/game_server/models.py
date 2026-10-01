"""Request payload models."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, field_validator

# `pass` is reserved: nothing can award it until presence-proof and visual checks exist.
VerdictStatus = Literal["failed", "pending", "pass"]
# `uncertain` and `skipped` are for the referee's visual checks; deterministic checks
# only ever pass or fail.
CheckOutcome = Literal["passed", "failed", "uncertain", "skipped"]


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


class ProximityHintRequest(BaseModel):
    """Body of `POST /checkpoint/proximity`: where the player says they are."""

    model_config = ConfigDict(extra="forbid")

    session: UUID
    participant: UUID
    checkpoint: int = Field(ge=1, strict=True, description="The checkpoint's `sequence`")
    location: Location


class ProximityHint(BaseModel):
    """Advisory answer: the only field, so nothing else about the checkpoint can leak."""

    model_config = ConfigDict(extra="forbid")

    in_range: bool


class PoseInstruction(BaseModel):
    """The pose a player must strike at a checkpoint: the only field, so nothing else leaks."""

    model_config = ConfigDict(extra="forbid")

    pose: str | None = Field(description="`null` when the checkpoint has no visual challenge")


CONSENT_TEXT = (
    "I agree to my photos and checkpoint locations being used as described to verify my "
    "progress in this game."
)


class JoinRequest(BaseModel):
    """Body of `POST /join`: a team's join code and the player's consent."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=64, description="The team's join code")
    # Strict: only JSON `true` records consent; false, "true", 1 and a missing field don't.
    consent: StrictBool = Field(description=f"Must be true: the player ticked: {CONSENT_TEXT}")

    @field_validator("consent")
    @classmethod
    def _must_consent(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("consent is required to join")
        return value


def _kebab(name: str) -> str:
    return name.replace("_", "-")


class JoinedSession(BaseModel):
    """What a team is told about its session: nothing it isn't told anyway."""

    model_config = ConfigDict(alias_generator=_kebab, validate_by_name=True)

    id: UUID
    name: str
    location: str
    start_time: datetime
    end_time: datetime


class JoinResponse(BaseModel):
    """The team's participant id, used on every later call. Never the code or its order."""

    participant: UUID
    team: str
    session: JoinedSession
    checkpoints: int = Field(description="How many checkpoints, for '1 of 3'-style progress")


class RejectionOut(BaseModel):
    """A reason the submission failed, safe to show the player."""

    code: str
    message: str


class CheckOut(BaseModel):
    """One check that ran, as shown to the player.

    Deliberately has no `detail`: moderator-only text can't be serialised by accident.
    """

    check: str
    outcome: CheckOutcome
    confidence: float
    reason: str


class CheckpointVerdict(BaseModel):
    """The server's verdict on one attempt at one checkpoint."""

    sequence: int
    attempt: int
    time: datetime = Field(description="When the server received the submission (UTC)")
    verdict: VerdictStatus
    checks: list[CheckOut] = Field(description="Every check that ran, in order")
    rejections: list[RejectionOut] = Field(description="The failed checks' rejections")


class Verdict(BaseModel):
    """Who submitted for which game, and the checkpoint verdict."""

    game: UUID
    participant: UUID
    checkpoint: CheckpointVerdict


class ChallengeVerdict(BaseModel):
    """Response to a received challenge."""

    verdict: Verdict
    image_id: UUID

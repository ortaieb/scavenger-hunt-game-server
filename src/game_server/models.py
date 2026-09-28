"""Request payload models."""

from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


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
    location: Location
    capture_time: AwareDatetime = Field(alias="capture-time")


class ChallengeAccepted(BaseModel):
    """Response to a successfully received challenge."""

    image_id: UUID

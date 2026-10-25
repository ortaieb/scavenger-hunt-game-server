"""A hunt laid out by hand: the organiser's own checkpoints, as a `ready` draft.

The checkpoints are held to the rules the agent's drafts are, the pins being their own
candidate places. From there the draft is reviewed and published like any other.
"""

from math import cos, radians
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from game_server.designer.osm import METRES_PER_DEGREE_LAT
from game_server.designer.review import DraftProblemsError, candidate
from game_server.designer.rules import check_draft
from game_server.drafts import (
    BoundingBox,
    DraftArea,
    DraftChallenge,
    DraftCheckpoint,
    DraftPlace,
    DraftRequest,
    RequestText,
)
from game_server.models import Location

MANUAL_RUNNER = "manual"
DEFAULT_PROXIMITY_M = 30
DEFAULT_MAX_WALK_KM = 10
BBOX_MARGIN_M = 100.0


def _kebab(name: str) -> str:
    return name.replace("_", "-")


class ManualCheckpoint(BaseModel):
    """A checkpoint the organiser laid out. Text and proximity limits are the rules', so
    breaking one answers `draft problems`."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    location: Location
    clue: str
    scene: str
    pose: str
    proximity: int = Field(default=DEFAULT_PROXIMITY_M, strict=True)


class ManualDraftRequest(BaseModel):
    """`POST /designer/drafts/manual`: the area, theme and the checkpoints in route order."""

    model_config = ConfigDict(extra="forbid", alias_generator=_kebab)

    area: RequestText
    theme: RequestText
    max_walk_km: float = Field(default=DEFAULT_MAX_WALK_KM, ge=0.5, le=10, strict=True)
    checkpoints: list[ManualCheckpoint] = Field(min_length=3, max_length=8)


def _bbox(locations: list[Location]) -> BoundingBox:
    """The pins' extent, widened by a margin on every side."""
    south = min(location.lat for location in locations)
    north = max(location.lat for location in locations)
    lat_margin = BBOX_MARGIN_M / METRES_PER_DEGREE_LAT
    # Widest east-west margin: the pole-ward edge, where a degree of longitude is shortest.
    long_margin = BBOX_MARGIN_M / (
        METRES_PER_DEGREE_LAT * cos(radians(max(abs(south), abs(north))))
    )
    return BoundingBox(
        south=south - lat_margin,
        west=min(location.long for location in locations) - long_margin,
        north=north + lat_margin,
        east=max(location.long for location in locations) + long_margin,
    )


def build_manual_draft(
    body: ManualDraftRequest, min_spacing_m: float
) -> tuple[DraftRequest, DraftArea, list[DraftCheckpoint]]:
    """The draft's request, area and checkpoints; `DraftProblemsError` if they break a rule."""
    request = DraftRequest.model_validate(
        {
            "area": body.area,
            "theme": body.theme,
            "checkpoints": len(body.checkpoints),
            "max-walk-km": body.max_walk_km,
        }
    )
    area = DraftArea(
        name=body.area, bbox=_bbox([c.location for c in body.checkpoints]), clipped=False
    )
    checkpoints = [
        DraftCheckpoint(
            position=position,
            place=DraftPlace(
                osm=f"manual/{position}", name=c.name, kind="manual", location=c.location
            ),
            clue=c.clue,
            challenge=DraftChallenge(scene=c.scene, pose=c.pose),
            proximity=c.proximity,
            rationale="",
        )
        for position, c in enumerate(body.checkpoints, start=1)
    ]
    candidates = [candidate(c.place) for c in checkpoints]
    problems = check_draft(checkpoints, candidates, request, area, min_spacing_m)
    if problems:
        raise DraftProblemsError(problems)
    return request, area, checkpoints

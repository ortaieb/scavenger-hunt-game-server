"""The rules a draft must meet: pure functions, no I/O.

The agent's `submit_draft` tool runs them and hands the problems back, so the agent fixes its
own draft before anyone sees it; the organiser's edits and publishing run them too, so an edit
can't break what the agent was held to. A problem's message is written for both: it may quote
the draft's own text, but never coordinates.

Coordinates always come from this run's candidate places, never from the model: a checkpoint
whose place isn't a candidate is `unknown_place`, and is left out of the distance rules.
"""

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from itertools import combinations
from math import cos, radians
from typing import Literal

from annotated_types import MaxLen, MinLen

from game_server.config import DEFAULT_DESIGNER_MIN_SPACING_M
from game_server.designer.osm import METRES_PER_DEGREE_LAT, Area, Place
from game_server.drafts import BoundingBox, DraftArea, DraftCheckpoint, DraftRequest, Problem
from game_server.geo import distance_m
from game_server.models import Location
from game_server.sessions import VisualChallenge

ProblemCode = Literal[
    "unknown_place",
    "duplicate_place",
    "wrong_count",
    "outside_area",
    "too_close",
    "route_too_long",
    "names_place",
    "empty",
    "too_long",
    "bad_proximity",
]

CLUE_MAX_LENGTH = 300  # it must fit a phone screen
PROXIMITY_MIN_M, PROXIMITY_MAX_M = 20, 100
AREA_MARGIN_M = 50.0
NODE_PROXIMITY_M, AREA_PROXIMITY_M = 30, 50
NAME_WORD_MIN_LETTERS = 5
GENERIC_WORDS = frozenset(
    {
        "street",
        "road",
        "park",
        "garden",
        "church",
        "house",
        "memorial",
        "statue",
        "fountain",
        "bridge",
        "tower",
        "gate",
        "green",
        "river",
        "chapel",
        "school",
        "court",
        "place",
        "square",
        "the",
    }
)


def _length_limits(field: str) -> tuple[int, int]:
    """A challenge field's limits, from the sessions file's model: one source for both."""
    metadata = VisualChallenge.model_fields[field].metadata
    low = next(item.min_length for item in metadata if isinstance(item, MinLen))
    high = next(item.max_length for item in metadata if isinstance(item, MaxLen))
    return low, high


SCENE_LIMITS = _length_limits("scene")
POSE_LIMITS = _length_limits("pose")


def _problem(code: ProblemCode, position: int | None, message: str) -> Problem:
    return Problem(code=code, position=position, message=message)


# --- the route -----------------------------------------------------------------------------


def route_legs(locations: Sequence[Location]) -> list[float]:
    """Metres from each location to the next, closing the loop back to the first.

    Each team walks its own rotation of the loop, minus one leg.
    """
    if not locations:
        return []
    following = [*locations[1:], locations[0]]
    return [distance_m(a, b) for a, b in zip(locations, following, strict=True)]


def default_proximity(place: Place) -> int:
    """30 m for a point; 50 m for a way or relation (a park, a large building)."""
    return NODE_PROXIMITY_M if place.osm.startswith("node/") else AREA_PROXIMITY_M


# --- one checkpoint ------------------------------------------------------------------------


def check_checkpoint(checkpoint: DraftCheckpoint, place: Place | None) -> list[Problem]:
    """The rules for one checkpoint, as an edit must keep them; `place` is its candidate."""
    position = checkpoint.position
    problems: list[Problem] = []
    if place is None:
        problems.append(
            _problem(
                "unknown_place",
                position,
                f"Checkpoint {position}'s place isn't one of this run's candidate places; "
                "pick one of them",
            )
        )
    problems += _text_problems(checkpoint)
    name = place.name if place is not None else checkpoint.place.name
    problems += _naming_problems(checkpoint, name)
    if not PROXIMITY_MIN_M <= checkpoint.proximity <= PROXIMITY_MAX_M:
        problems.append(
            _problem(
                "bad_proximity",
                position,
                f"Checkpoint {position}'s proximity is {checkpoint.proximity} m; keep it "
                f"between {PROXIMITY_MIN_M} and {PROXIMITY_MAX_M} m",
            )
        )
    return problems


def _text_problems(checkpoint: DraftCheckpoint) -> list[Problem]:
    """`empty` or `too_long` for the clue, the scene and the pose."""
    fields = (
        ("clue", checkpoint.clue, (1, CLUE_MAX_LENGTH)),
        ("scene", checkpoint.challenge.scene, SCENE_LIMITS),
        ("pose", checkpoint.challenge.pose, POSE_LIMITS),
    )
    problems = []
    for label, text, (low, high) in fields:
        position = checkpoint.position
        if len(text.strip()) < low:
            problems.append(
                _problem("empty", position, f"Checkpoint {position}'s {label} is empty")
            )
        elif len(text) > high:
            problems.append(
                _problem(
                    "too_long",
                    position,
                    f"Checkpoint {position}'s {label} is {len(text)} characters; "
                    f"keep it within {high}",
                )
            )
    return problems


def _fold(text: str) -> str:
    """Lower case, accents removed, every run of non-letters a single space."""
    decomposed = unicodedata.normalize("NFKD", text)
    letters = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.findall(r"[^\W\d_]+", letters.casefold()))


def giveaway_words(name: str) -> list[str]:
    """The name's distinctive words: 5 or more letters, and not on the generic list."""
    words = _fold(name).split()
    return [
        word
        for word in dict.fromkeys(words)
        if len(word) >= NAME_WORD_MIN_LETTERS and word not in GENERIC_WORDS
    ]


def names_in(text: str, name: str) -> str | None:
    """What in `text` gives the place away: its full name, or one of its distinctive words."""
    folded = f" {_fold(text)} "
    full = _fold(name)
    if full and f" {full} " in folded:
        return name
    return next((word for word in giveaway_words(name) if f" {word}" in folded), None)


def _naming_problems(checkpoint: DraftCheckpoint, name: str) -> list[Problem]:
    """`names_place` when the clue or the pose gives the place's name away."""
    problems = []
    for label, text in (("clue", checkpoint.clue), ("pose", checkpoint.challenge.pose)):
        found = names_in(text, name)
        if found is not None:
            problems.append(
                _problem(
                    "names_place",
                    checkpoint.position,
                    f"Checkpoint {checkpoint.position}'s {label} gives the place away "
                    f'("{found}"); describe it without its name',
                )
            )
    return problems


# --- the whole draft -----------------------------------------------------------------------


def check_draft(
    checkpoints: Sequence[DraftCheckpoint],
    candidates: Iterable[Place],
    request: DraftRequest,
    area: Area | DraftArea,
    min_spacing_m: float = DEFAULT_DESIGNER_MIN_SPACING_M,
) -> list[Problem]:
    """Every problem with the draft: the count, then each checkpoint, then spacing and route."""
    by_ref = {place.osm: place for place in candidates}
    ordered = sorted(checkpoints, key=lambda checkpoint: checkpoint.position)
    problems: list[Problem] = []
    if len(ordered) != request.checkpoints:
        problems.append(
            _problem(
                "wrong_count",
                None,
                f"The draft has {len(ordered)} checkpoints; {request.checkpoints} were asked for",
            )
        )
    placed: dict[int, Location] = {}
    seen: dict[str, int] = {}
    for checkpoint in ordered:
        place = by_ref.get(checkpoint.place.osm)
        problems += check_checkpoint(checkpoint, place)
        problems += _place_problems(checkpoint, place, seen, area.bbox)
        if place is not None:
            placed[checkpoint.position] = place.location
    problems += _spacing_problems(placed, min_spacing_m)
    problems += _route_problems(list(placed.values()), request.max_walk_km)
    return problems


def _place_problems(
    checkpoint: DraftCheckpoint,
    place: Place | None,
    seen: dict[str, int],
    bbox: BoundingBox,
) -> list[Problem]:
    """`duplicate_place` and `outside_area` for a checkpoint's place."""
    position, ref = checkpoint.position, checkpoint.place.osm
    problems = []
    if ref in seen:
        problems.append(
            _problem(
                "duplicate_place",
                position,
                f"Checkpoint {position} is the same place as checkpoint {seen[ref]}; "
                "pick a different place",
            )
        )
    else:
        seen[ref] = position
    if place is not None and not inside(place.location, bbox, AREA_MARGIN_M):
        problems.append(
            _problem(
                "outside_area",
                position,
                f"Checkpoint {position}'s place is outside the hunt's area; pick one inside it",
            )
        )
    return problems


def inside(location: Location, bbox: BoundingBox, margin_m: float) -> bool:
    """Whether the location is in the box, widened by `margin_m` on every side."""
    lat_margin = margin_m / METRES_PER_DEGREE_LAT
    long_margin = margin_m / (METRES_PER_DEGREE_LAT * cos(radians(location.lat)))
    return (
        bbox.south - lat_margin <= location.lat <= bbox.north + lat_margin
        and bbox.west - long_margin <= location.long <= bbox.east + long_margin
    )


def _spacing_problems(placed: Mapping[int, Location], min_spacing_m: float) -> list[Problem]:
    """`too_close` for each pair of checkpoints nearer than the minimum, at the later one."""
    problems = []
    for (first, a), (second, b) in combinations(sorted(placed.items()), 2):
        apart = distance_m(a, b)
        if apart < min_spacing_m:
            problems.append(
                _problem(
                    "too_close",
                    second,
                    f"Checkpoints {first} and {second} are {apart:.0f} m apart; keep them at "
                    f"least {min_spacing_m:.0f} m apart",
                )
            )
    return problems


def _route_problems(locations: Sequence[Location], max_walk_km: float) -> list[Problem]:
    """`route_too_long` when the closed loop is longer than the walk asked for."""
    loop_m = sum(route_legs(locations))
    if loop_m <= max_walk_km * 1000:
        return []
    return [
        _problem(
            "route_too_long",
            None,
            f"The route is {loop_m / 1000:.1f} km round; keep it within {max_walk_km:g} km",
        )
    ]

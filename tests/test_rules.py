"""The draft rules: pure functions, no I/O."""

from collections.abc import Callable
from math import pi

import pytest

from game_server.designer.osm import METRES_PER_DEGREE_LAT, Area, Place
from game_server.designer.rules import (
    CLUE_MAX_LENGTH,
    POSE_LIMITS,
    SCENE_LIMITS,
    check_checkpoint,
    check_draft,
    default_proximity,
    giveaway_words,
    names_in,
    route_legs,
)
from game_server.drafts import BoundingBox, DraftCheckpoint, DraftRequest, Problem
from game_server.geo import EARTH_RADIUS_M, distance_m
from game_server.models import Location
from game_server.sessions import VisualChallenge

CENTRE = Location(lat=51.4900, long=-0.2600)
AREA = Area(
    name="Chiswick",
    bbox=BoundingBox(south=51.4800, west=-0.2750, north=51.5000, east=-0.2450),
    centre=CENTRE,
    clipped=False,
)
HOUSE = Place(
    osm="node/1",
    name="Hogarth's House",
    kind="historic=building",
    location=Location(lat=51.4900, long=-0.2600),
    tags={},
)
CHURCH = Place(
    osm="way/2",
    name="Saint Nicholas Church",
    kind="amenity=place_of_worship",
    location=Location(lat=51.4936, long=-0.2600),  # 400 m north
    tags={},
)
GATE = Place(
    osm="relation/3",
    name="Fuller's Brewery Gate",
    kind="historic=city_gate",
    location=Location(lat=51.4918, long=-0.2545),  # about 420 m from each
    tags={},
)
CANDIDATES = [HOUSE, CHURCH, GATE]
REQUEST = DraftRequest.model_validate({"area": "Chiswick", "theme": "Painters and brewers"})


def checkpoint(position: int, place: Place, **changes: object) -> DraftCheckpoint:
    values: dict[str, object] = {
        "position": position,
        "place": place.draft_place().model_dump(),
        "clue": f"Clue {position}: find the painter's old home by the busy road.",
        "challenge": {"scene": f"Scene {position}: a brick front behind railings.", "pose": "Wave"},
        "proximity": 40,
        "rationale": "A good spot.",
    }
    values.update(changes)
    return DraftCheckpoint.model_validate(values)


def clean() -> list[DraftCheckpoint]:
    return [checkpoint(1, HOUSE), checkpoint(2, CHURCH), checkpoint(3, GATE)]


def at(problems: list[Problem], code: str) -> list[int | None]:
    """The positions of the problems with this code."""
    return [problem.position for problem in problems if problem.code == code]


def moved(place: Place, lat: float, long: float) -> Place:
    return place.model_copy(update={"location": Location(lat=lat, long=long)})


# --- a clean draft -------------------------------------------------------------------------


def test_a_clean_draft_has_no_problems() -> None:
    assert check_draft(clean(), CANDIDATES, REQUEST, AREA) == []


def test_the_draft_s_area_shape_works_too() -> None:
    assert check_draft(clean(), CANDIDATES, REQUEST, AREA.draft_area()) == []


def test_checkpoints_are_checked_in_position_order() -> None:
    draft = clean()[::-1]

    assert check_draft(draft, CANDIDATES, REQUEST, AREA) == []


# --- each rule, on the whole draft ---------------------------------------------------------

Change = Callable[[], tuple[list[DraftCheckpoint], list[Place], DraftRequest]]


def unknown_place() -> tuple[list[DraftCheckpoint], list[Place], DraftRequest]:
    stranger = CHURCH.model_copy(update={"osm": "node/999"})
    return [checkpoint(1, HOUSE), checkpoint(2, stranger), checkpoint(3, GATE)], CANDIDATES, REQUEST


def duplicate_place() -> tuple[list[DraftCheckpoint], list[Place], DraftRequest]:
    return [checkpoint(1, HOUSE), checkpoint(2, CHURCH), checkpoint(3, HOUSE)], CANDIDATES, REQUEST


def wrong_count() -> tuple[list[DraftCheckpoint], list[Place], DraftRequest]:
    return clean(), CANDIDATES, REQUEST.model_copy(update={"checkpoints": 4})


def outside_area() -> tuple[list[DraftCheckpoint], list[Place], DraftRequest]:
    far_gate = moved(GATE, 51.4918, -0.2440)  # about 70 m east of the box
    return clean(), [HOUSE, CHURCH, far_gate], REQUEST


def too_close() -> tuple[list[DraftCheckpoint], list[Place], DraftRequest]:
    near_church = moved(CHURCH, 51.4909, -0.2600)  # about 100 m from the house
    return clean(), [HOUSE, near_church, GATE], REQUEST


def route_too_long() -> tuple[list[DraftCheckpoint], list[Place], DraftRequest]:
    return clean(), CANDIDATES, REQUEST.model_copy(update={"max_walk_km": 1.0})


@pytest.mark.parametrize(
    ("change", "code", "positions"),
    [
        (unknown_place, "unknown_place", [2]),
        (duplicate_place, "duplicate_place", [3]),
        (wrong_count, "wrong_count", [None]),
        (outside_area, "outside_area", [3]),
        (too_close, "too_close", [2]),
        (route_too_long, "route_too_long", [None]),
    ],
    ids=lambda value: value.__name__ if callable(value) else None,
)
def test_each_draft_rule(change: Change, code: str, positions: list[int | None]) -> None:
    checkpoints, candidates, request = change()

    assert at(check_draft(checkpoints, candidates, request, AREA), code) == positions


def test_an_unknown_place_is_left_out_of_the_distance_rules() -> None:
    stranger = HOUSE.model_copy(update={"osm": "node/999"})  # where the house is
    draft = [checkpoint(1, stranger), checkpoint(2, CHURCH), checkpoint(3, GATE)]
    beside_the_church = moved(HOUSE, 51.4935, -0.2600)
    candidates = [beside_the_church, CHURCH, GATE]  # its coordinates are never used

    problems = check_draft(draft, candidates, REQUEST, AREA)

    assert [p.code for p in problems] == ["unknown_place"]


def test_coordinates_come_from_the_candidate_not_the_draft() -> None:
    # The draft claims the gate is far outside the area; the candidate knows where it is.
    lying = checkpoint(3, moved(GATE, 0.0, 0.0))

    assert check_draft([*clean()[:2], lying], CANDIDATES, REQUEST, AREA) == []


def test_the_area_has_a_fifty_metre_margin() -> None:
    just_outside = moved(GATE, 51.4918, -0.2445)  # about 35 m east of the box

    assert check_draft(clean(), [HOUSE, CHURCH, just_outside], REQUEST, AREA) == []


def test_every_pair_is_spaced_not_just_neighbours() -> None:
    # Checkpoints 1 and 3 aren't neighbours on the route, but they're 60 m apart.
    near_house = moved(GATE, 51.4900, -0.2591)
    candidates = [HOUSE, CHURCH, near_house]

    problems = [p for p in check_draft(clean(), candidates, REQUEST, AREA) if p.code == "too_close"]

    assert [(p.position, p.message) for p in problems] == [
        (3, "Checkpoints 1 and 3 are 62 m apart; keep them at least 150 m apart")
    ]


def test_the_minimum_spacing_is_a_parameter() -> None:
    problems = check_draft(clean(), CANDIDATES, REQUEST, AREA, min_spacing_m=410)

    assert at(problems, "too_close") == [2]


def test_the_route_is_measured_as_a_closed_loop() -> None:
    loop_km = sum(route_legs([HOUSE.location, CHURCH.location, GATE.location])) / 1000
    just_enough = REQUEST.model_copy(update={"max_walk_km": round(loop_km + 0.01, 2)})
    too_little = REQUEST.model_copy(update={"max_walk_km": round(loop_km - 0.01, 2)})

    assert check_draft(clean(), CANDIDATES, just_enough, AREA) == []
    assert at(check_draft(clean(), CANDIDATES, too_little, AREA), "route_too_long") == [None]


def test_messages_never_give_coordinates() -> None:
    draft = [
        checkpoint(1, HOUSE, clue="Hogarth lived here."),
        checkpoint(2, CHURCH.model_copy(update={"osm": "node/999"}), proximity=5),
        checkpoint(3, HOUSE),
    ]

    problems = check_draft(draft, [HOUSE, moved(CHURCH, 51.6, -0.1), GATE], REQUEST, AREA)

    assert {p.code for p in problems} >= {"names_place", "unknown_place", "duplicate_place"}
    for problem in problems:
        for coordinate in ("51.4", "51.5", "-0.2", "0.26"):
            assert coordinate not in problem.message


# --- each rule, on one checkpoint ----------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        pytest.param({"clue": "Visit Hogarth's house."}, "names_place", id="clue-names-a-word"),
        pytest.param({"clue": "HOGARTH painted here."}, "names_place", id="other-case"),
        pytest.param({"clue": "A clue about hogarths."}, "names_place", id="word-with-suffix"),
        pytest.param(
            {"challenge": {"scene": "A door.", "pose": "Paint like Hogarth"}},
            "names_place",
            id="pose",
        ),
        pytest.param({"clue": " \n "}, "empty", id="blank-clue"),
        pytest.param({"challenge": {"scene": "", "pose": "Wave"}}, "empty", id="empty-scene"),
        pytest.param({"challenge": {"scene": "A door.", "pose": "  "}}, "empty", id="blank-pose"),
        pytest.param({"clue": "x" * (CLUE_MAX_LENGTH + 1)}, "too_long", id="long-clue"),
        pytest.param(
            {"challenge": {"scene": "x" * (SCENE_LIMITS[1] + 1), "pose": "Wave"}},
            "too_long",
            id="long-scene",
        ),
        pytest.param(
            {"challenge": {"scene": "A door.", "pose": "x" * (POSE_LIMITS[1] + 1)}},
            "too_long",
            id="long-pose",
        ),
        pytest.param({"proximity": 19}, "bad_proximity", id="proximity-too-small"),
        pytest.param({"proximity": 101}, "bad_proximity", id="proximity-too-large"),
    ],
)
def test_each_checkpoint_rule(changes: dict[str, object], code: str) -> None:
    draft = [checkpoint(1, HOUSE, **changes), checkpoint(2, CHURCH), checkpoint(3, GATE)]

    assert at(check_draft(draft, CANDIDATES, REQUEST, AREA), code) == [1]
    assert at(check_checkpoint(draft[0], HOUSE), code) == [1]


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"clue": "x" * CLUE_MAX_LENGTH}, id="clue-at-max"),
        pytest.param(
            {"challenge": {"scene": "x" * SCENE_LIMITS[1], "pose": "Wave"}}, id="scene-at-max"
        ),
        pytest.param(
            {"challenge": {"scene": "A door.", "pose": "x" * POSE_LIMITS[1]}}, id="pose-at-max"
        ),
        pytest.param({"proximity": 20}, id="proximity-at-min"),
        pytest.param({"proximity": 100}, id="proximity-at-max"),
        pytest.param({"clue": "Find the old house by the road."}, id="only-generic-words"),
        pytest.param({"clue": "Find the brewer's arch."}, id="another-place-s-word"),
    ],
)
def test_limits_are_inclusive_and_generic_words_pass(changes: dict[str, object]) -> None:
    assert check_checkpoint(checkpoint(1, HOUSE, **changes), HOUSE) == []


def test_the_limits_come_from_the_sessions_file() -> None:
    scene = VisualChallenge.model_fields["scene"].metadata
    pose = VisualChallenge.model_fields["pose"].metadata

    assert (scene[0].min_length, scene[1].max_length) == SCENE_LIMITS
    assert (pose[0].min_length, pose[1].max_length) == POSE_LIMITS


def test_a_passing_challenge_can_be_published() -> None:
    at_max = checkpoint(
        1, HOUSE, challenge={"scene": "x" * SCENE_LIMITS[1], "pose": "y" * POSE_LIMITS[1]}
    )

    assert check_checkpoint(at_max, HOUSE) == []
    VisualChallenge(scene=at_max.challenge.scene, pose=at_max.challenge.pose)


def test_an_unknown_place_on_its_own() -> None:
    [problem] = check_checkpoint(checkpoint(2, CHURCH), None)

    assert (problem.code, problem.position) == ("unknown_place", 2)


def test_editing_checks_only_the_checkpoint() -> None:
    # Too close to the house and on a long route, but an edit isn't judged on those.
    near = moved(CHURCH, 51.4901, -0.2600)

    assert check_checkpoint(checkpoint(2, near), near) == []


def test_a_message_names_the_field_and_the_word() -> None:
    [problem] = check_checkpoint(checkpoint(1, HOUSE, clue="Hogarth lived here."), HOUSE)

    assert problem.message == (
        'Checkpoint 1\'s clue gives the place away ("hogarth"); describe it without its name'
    )


# --- names_place ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "words"),
    [
        ("Hogarth's House", ["hogarth"]),
        ("Saint Nicholas Church", ["saint", "nicholas"]),
        ("The Old Green Bridge", []),
        ("Kew Bridge", []),
        ("Château Lumière", ["chateau", "lumiere"]),
    ],
)
def test_giveaway_words(name: str, words: list[str]) -> None:
    assert giveaway_words(name) == words


@pytest.mark.parametrize(
    ("text", "name", "found"),
    [
        ("Find the CHATEAU of light", "Château Lumière", "chateau"),
        ("Find the château", "Chateau Lumiere", "chateau"),
        ("Meet on the green.", "The Green", "The Green"),
        ("Cross kew bridge.", "Kew Bridge", "Kew Bridge"),
        ("Cross near Kew.", "Kew Bridge", None),
        ("The bridge by the river.", "Kew Bridge", None),
        ("A saintly figure.", "Saint Nicholas Church", "saint"),
        ("Unsaintly.", "Saint Nicholas Church", None),
    ],
)
def test_names_in(text: str, name: str, found: str | None) -> None:
    assert names_in(text, name) == found


# --- the route and proximity ---------------------------------------------------------------


def test_route_legs_close_the_loop() -> None:
    step = 500 / (EARTH_RADIUS_M * pi / 180)  # 500 m of latitude, in degrees
    points = [Location(lat=51.0 + n * step, long=-0.2) for n in range(3)]

    assert route_legs(points) == [
        pytest.approx(500),
        pytest.approx(500),
        pytest.approx(1000),
    ]


def test_route_legs_of_one_and_none() -> None:
    assert route_legs([CENTRE]) == [0.0]
    assert route_legs([]) == []


def test_route_legs_use_the_server_s_distance() -> None:
    legs = route_legs([HOUSE.location, CHURCH.location])

    assert legs == [distance_m(HOUSE.location, CHURCH.location)] * 2


@pytest.mark.parametrize(
    ("osm", "proximity"),
    [("node/1", 30), ("way/2", 50), ("relation/3", 50)],
)
def test_default_proximity(osm: str, proximity: int) -> None:
    assert default_proximity(HOUSE.model_copy(update={"osm": osm})) == proximity


def test_metres_per_degree_matches_the_distance() -> None:
    north = Location(lat=CENTRE.lat + 1000 / METRES_PER_DEGREE_LAT, long=CENTRE.long)

    assert distance_m(CENTRE, north) == pytest.approx(1000, rel=1e-3)

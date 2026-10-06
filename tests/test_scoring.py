from datetime import UTC, datetime, timedelta

import pytest

from game_server.scoring import (
    SessionResults,
    TeamPoints,
    checkpoint_place,
    places,
    results_final,
    team_points,
)
from game_server.session_runs import SessionPhase
from game_server.sessions import Team

T0 = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
FOXES = Team(name="Foxes", join_code="FOX-7Q2K", order=(1, 2, 3))
HERONS = Team(name="Herons", join_code="HERON-4MXP", order=(3, 1, 2))


def minutes(n: int) -> datetime:
    return T0 + timedelta(minutes=n)


def results(
    joined: set[str],
    passes: dict[tuple[str, int], datetime] | None = None,
    pending: set[tuple[str, int]] | None = None,
) -> SessionResults:
    return SessionResults(frozenset(joined), passes or {}, frozenset(pending or set()))


# --- a place at one checkpoint ----------------------------------------------------------


@pytest.mark.parametrize(
    ("team", "place"),
    [("A", 1), ("B", 2), ("C", 3), ("D", None)],
    ids=["first", "second", "third", "no-pass"],
)
def test_place_is_the_order_photos_were_received(team: str, place: int | None) -> None:
    passed = results(
        {"A", "B", "C", "D"}, {("C", 1): minutes(9), ("A", 1): minutes(1), ("B", 1): minutes(5)}
    )

    assert checkpoint_place(passed, team, 1) == place


def test_equal_times_share_a_place() -> None:
    passed = results(
        {"A", "B", "C"}, {("A", 1): minutes(1), ("B", 1): minutes(1), ("C", 1): minutes(2)}
    )

    assert [checkpoint_place(passed, team, 1) for team in "ABC"] == [1, 1, 3]


def test_other_checkpoints_do_not_affect_a_place() -> None:
    passed = results({"A", "B"}, {("A", 2): minutes(0), ("B", 1): minutes(5)})

    assert checkpoint_place(passed, "B", 1) == 1


def test_pending_photos_take_no_place() -> None:
    mixed = results({"A", "B"}, {("B", 1): minutes(5)}, {("A", 1)})

    assert checkpoint_place(mixed, "B", 1) == 1


# --- a team's points ---------------------------------------------------------------------


def test_nothing_done_counts_teams_joined_plus_one_everywhere() -> None:
    assert team_points(FOXES, results({"Foxes"})) == TeamPoints(points=2 * 3, in_review=0)
    assert team_points(FOXES, results({"Foxes", "Herons", "Owls"})) == TeamPoints(4 * 3, 0)


def test_points_add_places_and_unplaced_checkpoints() -> None:
    passed = results(
        {"Foxes", "Herons"},
        {("Herons", 1): minutes(1), ("Foxes", 1): minutes(2), ("Foxes", 3): minutes(3)},
    )

    assert team_points(FOXES, passed) == TeamPoints(points=2 + 3 + 1, in_review=0)
    assert team_points(HERONS, passed) == TeamPoints(points=3 + 1 + 3, in_review=0)


def test_pending_counts_unplaced_and_in_review() -> None:
    mixed = results({"Foxes", "Herons"}, pending={("Foxes", 1), ("Foxes", 2)})

    assert team_points(FOXES, mixed) == TeamPoints(points=3 * 3, in_review=2)


def test_a_pass_after_a_pending_is_not_in_review() -> None:
    mixed = results({"Foxes"}, {("Foxes", 1): minutes(1)}, {("Foxes", 1)})

    assert team_points(FOXES, mixed) == TeamPoints(points=1 + 2 + 2, in_review=0)


def test_checkpoints_off_the_route_do_not_count() -> None:
    short = Team(name="Foxes", join_code="FOX-7Q2K", order=(1,))
    passed = results({"Foxes"}, {("Foxes", 1): minutes(1), ("Foxes", 2): minutes(2)})

    assert team_points(short, passed) == TeamPoints(points=1, in_review=0)


# --- final places ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("phase", "to_review", "final"),
    [
        ("stopped", 0, True),
        ("stopped", 1, False),
        ("running", 0, False),
        ("scheduled", 0, False),
    ],
    ids=["stopped-and-reviewed", "stopped-with-a-photo-to-review", "running", "scheduled"],
)
def test_results_are_final_once_stopped_and_reviewed(
    phase: SessionPhase, to_review: int, final: bool
) -> None:
    reviewed = SessionResults(frozenset({"A"}), {}, frozenset(), to_review=to_review)

    assert results_final(reviewed, phase) is final


@pytest.mark.parametrize(
    ("points", "expected"),
    [
        ({"A": 5}, {"A": 1}),
        ({"A": 7, "B": 5, "C": 9}, {"A": 2, "B": 1, "C": 3}),
        ({"A": 5, "B": 5, "C": 9}, {"A": 1, "B": 1, "C": 3}),
        ({"A": 5, "B": 7, "C": 7, "D": 8}, {"A": 1, "B": 2, "C": 2, "D": 4}),
        ({}, {}),
    ],
    ids=["alone", "ordered", "tie-for-first", "tie-in-the-middle", "nobody"],
)
def test_places_lowest_first_with_shared_ties(
    points: dict[str, int], expected: dict[str, int]
) -> None:
    assert places(points) == expected

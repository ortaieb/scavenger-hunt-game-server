from math import pi

import pytest

from game_server.geo import EARTH_RADIUS_M, distance_m
from game_server.models import Location

# Reference distances are WGS84 ellipsoid geodesics from GeographicLib (Karney, 2013),
# computed independently of this code. A sphere is within 0.5 % of the ellipsoid.
TOLERANCE = 0.005


@pytest.mark.parametrize(
    ("a", "b", "reference_m"),
    [
        pytest.param((51.5007, -0.1246), (48.8584, 2.2945), 340_894.8, id="big-ben-eiffel"),
        pytest.param((40.6413, -73.7781), (33.9416, -118.4085), 3_983_079.7, id="jfk-lax"),
        pytest.param((-33.8688, 151.2093), (-36.8485, 174.7633), 2_160_508.8, id="sydney-auckland"),
        pytest.param((0.0, 179.9), (0.0, -179.9), 22_263.9, id="across-antimeridian"),
        pytest.param((89.9, 0.0), (89.9, 180.0), 22_338.8, id="across-north-pole"),
        pytest.param((51.5, -0.1), (51.50045, -0.1), 50.07, id="about-50m-north"),
        pytest.param((51.5, -0.1), (51.5003, -0.10048), 47.17, id="about-50m-diagonal"),
    ],
)
def test_matches_reference_geodesics(
    a: tuple[float, float], b: tuple[float, float], reference_m: float
) -> None:
    measured = distance_m(Location(lat=a[0], long=a[1]), Location(lat=b[0], long=b[1]))

    assert measured == pytest.approx(reference_m, rel=TOLERANCE)


def test_same_point_is_zero() -> None:
    point = Location(lat=51.509948, long=-1.485923)

    assert distance_m(point, point) == 0


def test_is_symmetric() -> None:
    a, b = Location(lat=51.5, long=-0.1), Location(lat=48.85, long=2.29)

    assert distance_m(a, b) == pytest.approx(distance_m(b, a))


def test_antimeridian_is_the_short_way_round() -> None:
    east, west = Location(lat=10, long=179.9), Location(lat=10, long=-179.9)

    assert distance_m(east, west) < 25_000


def test_antipodal_points_are_half_the_circumference() -> None:
    assert distance_m(Location(lat=0, long=0), Location(lat=0, long=180)) == pytest.approx(
        pi * EARTH_RADIUS_M
    )


def test_one_degree_of_latitude() -> None:
    one_degree = distance_m(Location(lat=0, long=0), Location(lat=1, long=0))

    assert one_degree == pytest.approx(2 * pi * EARTH_RADIUS_M / 360)

"""Great-circle distance between coordinates."""

from math import asin, cos, radians, sin, sqrt

from game_server.models import Location

# IUGG mean Earth radius. The spherical model is within ~0.5 % of the WGS84 ellipsoid,
# far below the tens-to-hundreds of metres a checkpoint's proximity is measured in.
EARTH_RADIUS_M = 6_371_008.8


def distance_m(a: Location, b: Location) -> float:
    """Haversine great-circle distance from `a` to `b`, in metres."""
    lat_a, lat_b = radians(a.lat), radians(b.lat)
    d_lat = lat_b - lat_a
    d_long = radians(b.long - a.long)
    h = sin(d_lat / 2) ** 2 + cos(lat_a) * cos(lat_b) * sin(d_long / 2) ** 2
    # min() guards asin against h creeping above 1 through rounding for antipodal points.
    return 2 * EARTH_RADIUS_M * asin(min(1.0, sqrt(h)))

"""Map data from OpenStreetMap: find an area by name, and the candidate places in it.

Nominatim turns "Chiswick, London" into a bounding box; Overpass lists the named, public
places inside it. The agent never calls these services itself: its tools call these
functions, so the server, not the model, holds every place's real coordinates.

The public instances have usage policies, and breaking them gets the server blocked: every
request identifies itself, Nominatim gets at most one request a second, every request has a
timeout, a `429` or `504` is retried once after a backoff, and results are cached for an hour.
Data © OpenStreetMap contributors, under the ODbL.
"""

import logging
import threading
import time
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import version
from math import cos, radians
from typing import Literal, Protocol

import httpx2
from pydantic import BaseModel, ConfigDict, JsonValue

from game_server.config import Settings, get_settings
from game_server.drafts import BoundingBox, DraftArea, DraftPlace
from game_server.geo import distance_m
from game_server.models import Location

logger = logging.getLogger(__name__)

USER_AGENT = (
    f"scavenger-hunt-game-server/{version('game-server')}"
    " (+https://github.com/ortaieb/scavenger-hunt-game-server)"
)
ATTRIBUTION = "© OpenStreetMap contributors"
NOMINATIM_INTERVAL_SECONDS = 1.0
RETRY_STATUSES = frozenset({429, 504})
RETRY_BACKOFF_SECONDS = 2.0
CACHE_SECONDS = 3600.0
METRES_PER_DEGREE_LAT = 111_195.0

# The kinds of place a hunt may use: tag key → allowed values (None: any value, as long as
# the place also has one of BUILDING_NOTABLE). Order decides a place's `kind` when several match.
KINDS: dict[str, frozenset[str] | None] = {
    "historic": frozenset(
        {
            "memorial",
            "monument",
            "building",
            "boundary_stone",
            "milestone",
            "wayside_cross",
            "wayside_shrine",
            "city_gate",
            "ruins",
        }
    ),
    "tourism": frozenset({"artwork", "attraction", "viewpoint", "museum"}),
    "amenity": frozenset({"fountain", "clock", "place_of_worship"}),
    "man_made": frozenset({"obelisk", "lighthouse", "water_tower", "bridge"}),
    "leisure": frozenset({"park", "garden"}),
    "building": None,
}
BUILDING_NOTABLE = ("heritage", "wikidata")
PRIVATE_ACCESS = frozenset({"private", "no", "customers"})
# Where children are: never a place to send players with cameras.
WITH_CHILDREN = {
    "amenity": frozenset({"school", "kindergarten", "childcare"}),
    "building": frozenset({"school", "kindergarten"}),
    "leisure": frozenset({"playground"}),
}
TAGS_KEPT = frozenset(
    {
        "name",
        "inscription",
        "description",
        "wikipedia",
        "wikidata",
        "start_date",
        "artist_name",
        "heritage",
        "memorial",
        "historic",
        "tourism",
        "amenity",
        "material",
        "subject",
    }
)
DOCUMENTED = ("wikipedia", "wikidata", "inscription")
MAX_PLACES = 200
SAME_PLACE_M = 30.0

Service = Literal["nominatim", "overpass"]
# The box's south, west, north and east, and the kinds searched for.
PlacesKey = tuple[tuple[float, float, float, float], tuple[str, ...]]
MapErrorCode = Literal["map_unavailable", "map_timeout"]


class MapDataError(Exception):
    """The map data couldn't be had: `map_unavailable` or `map_timeout`."""

    def __init__(self, code: MapErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class Area(BaseModel):
    """An area found by name: its box, the place's own point, and whether it was clipped."""

    model_config = ConfigDict(frozen=True)

    name: str
    bbox: BoundingBox
    centre: Location
    clipped: bool

    def draft_area(self) -> DraftArea:
        """The area as a draft shows it."""
        return DraftArea(name=self.name, bbox=self.bbox, clipped=self.clipped)


class Place(BaseModel):
    """A candidate place: its OSM id, name, the kind that matched, location and some tags."""

    model_config = ConfigDict(frozen=True)

    osm: str
    name: str
    kind: str
    location: Location
    tags: dict[str, str]

    def draft_place(self) -> DraftPlace:
        """The place as a draft checkpoint shows it."""
        return DraftPlace(osm=self.osm, name=self.name, kind=self.kind, location=self.location)


# --- the thin HTTP client: the only code that touches httpx2 ------------------------------


@dataclass(frozen=True)
class HttpReply:
    """A response's status and, when it's JSON, its body."""

    status: int
    body: JsonValue


class HttpTimeoutError(Exception):
    """The request timed out."""


class HttpConnectionError(Exception):
    """The request couldn't be made: DNS, connection refused, reset."""


class HttpClient(Protocol):
    """Makes one request. Tests replace it; nothing else depends on httpx2."""

    def request(
        self,
        method: Literal["GET", "POST"],
        url: str,
        *,
        params: Mapping[str, str] | None,
        data: Mapping[str, str] | None,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpReply:
        """Send it; raise `HttpTimeoutError` or `HttpConnectionError` if there's no reply."""
        ...


class Httpx2Client:
    """`HttpClient` on httpx2."""

    def __init__(self) -> None:
        self._client = httpx2.Client()

    def request(
        self,
        method: Literal["GET", "POST"],
        url: str,
        *,
        params: Mapping[str, str] | None,
        data: Mapping[str, str] | None,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpReply:
        """Send it; the body is parsed as JSON when it is JSON."""
        try:
            response = self._client.request(
                method, url, params=params, data=data, headers=headers, timeout=timeout
            )
        except httpx2.TimeoutException as exc:
            raise HttpTimeoutError(str(exc)) from exc
        except httpx2.TransportError as exc:
            raise HttpConnectionError(str(exc)) from exc
        is_json = response.headers.get("content-type", "").startswith("application/json")
        return HttpReply(response.status_code, response.json() if is_json else None)


# --- the OpenStreetMap client -------------------------------------------------------------


class TtlCache[K: Hashable, V]:
    """Values kept for a while, by the given clock."""

    def __init__(self, seconds: float, monotonic: Callable[[], float]) -> None:
        self._seconds = seconds
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._entries: dict[K, tuple[float, V]] = {}

    def get(self, key: K) -> tuple[V] | None:
        """`(value,)` if it's cached and fresh (the value may be None); else None."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry[0] <= self._monotonic():
                return None
            return (entry[1],)

    def put(self, key: K, value: V) -> None:
        """Keep `value` until the time is up."""
        with self._lock:
            self._entries[key] = (self._monotonic() + self._seconds, value)


class OsmClient:
    """Finds areas and places, politely: rate-limited, retried once, cached, logged."""

    def __init__(
        self,
        http: HttpClient,
        *,
        nominatim_url: str,
        overpass_url: str,
        timeout_seconds: float,
        max_area_km: float,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http
        self._nominatim_url = nominatim_url.rstrip("/")
        self._overpass_url = overpass_url
        self._timeout = timeout_seconds
        self._max_area_km = max_area_km
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_nominatim: float | None = None
        self._areas = TtlCache[str, Area | None](CACHE_SECONDS, monotonic)
        self._places = TtlCache[PlacesKey, tuple[Place, ...]](CACHE_SECONDS, monotonic)

    def find_area(self, query: str) -> Area | None:
        """The area best matching `query`, clipped to the size limit; None if nothing matches."""
        key = " ".join(query.casefold().split())
        cached = self._areas.get(key)
        if cached is not None:
            return cached[0]
        params = {"q": query, "format": "jsonv2", "limit": "1"}
        body = self._request("nominatim", "GET", f"{self._nominatim_url}/search", params=params)
        area = parse_area(body, self._max_area_km)
        self._areas.put(key, area)
        return area

    def find_places(self, area: Area, kinds: Sequence[str] | None = None) -> list[Place]:
        """The named, public places of the allowed kinds in the area, best-documented first."""
        allowed = narrow_kinds(kinds)
        box = area.bbox
        key = ((box.south, box.west, box.north, box.east), tuple(sorted(allowed_key(allowed))))
        cached = self._places.get(key)
        if cached is not None:
            return list(cached[0])
        query = overpass_query(area.bbox, allowed)
        body = self._request("overpass", "POST", self._overpass_url, data={"data": query})
        places = parse_places(body, allowed)
        self._places.put(key, tuple(places))
        return places

    def _request(
        self,
        service: Service,
        method: Literal["GET", "POST"],
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
    ) -> JsonValue:
        """Send the request, retrying once after a backoff on `429` or `504`."""
        reply = self._attempt(service, method, url, params, data)
        if reply.status in RETRY_STATUSES:
            self._sleep(RETRY_BACKOFF_SECONDS)
            reply = self._attempt(service, method, url, params, data)
        if reply.status != 200:
            raise MapDataError("map_unavailable", f"{service} answered {reply.status}")
        return reply.body

    def _attempt(
        self,
        service: Service,
        method: Literal["GET", "POST"],
        url: str,
        params: Mapping[str, str] | None,
        data: Mapping[str, str] | None,
    ) -> HttpReply:
        """One request, at Nominatim's rate, logged; transport failures become MapDataError."""
        if service == "nominatim":
            self._wait_for_nominatim()
        started = self._monotonic()
        headers = {"User-Agent": USER_AGENT}
        try:
            reply = self._http.request(
                method, url, params=params, data=data, headers=headers, timeout=self._timeout
            )
        except HttpTimeoutError:
            _log(service, "timeout", None, started, self._monotonic())
            raise MapDataError("map_timeout", f"{service} timed out") from None
        except HttpConnectionError:
            _log(service, "unreachable", None, started, self._monotonic())
            raise MapDataError("map_unavailable", f"{service} is unreachable") from None
        _log(service, str(reply.status), _count(reply.body), started, self._monotonic())
        return reply

    def _wait_for_nominatim(self) -> None:
        """At most one Nominatim request a second, across threads."""
        with self._lock:
            if self._last_nominatim is not None:
                wait = self._last_nominatim + NOMINATIM_INTERVAL_SECONDS - self._monotonic()
                if wait > 0:
                    self._sleep(wait)
            self._last_nominatim = self._monotonic()


def _count(body: JsonValue) -> int | None:
    if isinstance(body, list):
        return len(body)
    elements = body.get("elements") if isinstance(body, dict) else None
    return len(elements) if isinstance(elements, list) else None


def _log(service: Service, status: str, results: int | None, started: float, ended: float) -> None:
    """One line per request: never the query."""
    logger.info(
        "Map request %s status %s results %s duration_ms %d",
        service,
        status,
        "-" if results is None else results,
        round((ended - started) * 1000),
    )


# --- areas ---------------------------------------------------------------------------------


def parse_area(body: JsonValue, max_area_km: float) -> Area | None:
    """The first Nominatim result as an Area, clipped to `max_area_km`; None if there's none."""
    if not isinstance(body, list) or not body or not isinstance(body[0], dict):
        return None
    first = body[0]
    box = first.get("boundingbox")
    if not isinstance(box, list) or len(box) != 4:
        return None
    south, north, west, east = (float(str(value)) for value in box)  # Nominatim's order
    centre = Location(lat=float(str(first["lat"])), long=float(str(first["lon"])))
    bbox = BoundingBox(south=south, west=west, north=north, east=east)
    return clip(str(first["display_name"]), bbox, centre, max_area_km)


def clip(name: str, bbox: BoundingBox, centre: Location, max_area_km: float) -> Area:
    """The area, cut down around its centre in each direction wider than `max_area_km`."""
    limit_m = max_area_km * 1000
    height = distance_m(
        Location(lat=bbox.south, long=centre.long), Location(lat=bbox.north, long=centre.long)
    )
    width = distance_m(
        Location(lat=centre.lat, long=bbox.west), Location(lat=centre.lat, long=bbox.east)
    )
    if height <= limit_m and width <= limit_m:
        return Area(name=name, bbox=bbox, centre=centre, clipped=False)
    half_lat = limit_m / 2 / METRES_PER_DEGREE_LAT
    half_long = limit_m / 2 / (METRES_PER_DEGREE_LAT * cos(radians(centre.lat)))
    clipped = BoundingBox(
        south=centre.lat - half_lat if height > limit_m else bbox.south,
        north=centre.lat + half_lat if height > limit_m else bbox.north,
        west=centre.long - half_long if width > limit_m else bbox.west,
        east=centre.long + half_long if width > limit_m else bbox.east,
    )
    return Area(name=name, bbox=clipped, centre=centre, clipped=True)


# --- places --------------------------------------------------------------------------------

Allowed = dict[str, frozenset[str] | None]


def narrow_kinds(kinds: Sequence[str] | None) -> Allowed:
    """The allowlist, narrowed to `kinds` (`historic` or `tourism=artwork`); all by default.

    A kind outside the allowlist is a `ValueError`.
    """
    if kinds is None:
        return dict(KINDS)
    allowed: Allowed = {}
    for kind in kinds:
        key, _, value = kind.partition("=")
        if key not in KINDS:
            raise ValueError(f"not an allowed kind of place: {kind}")
        values = KINDS[key]
        if not value:
            allowed[key] = values
        elif values is None or value not in values:
            raise ValueError(f"not an allowed kind of place: {kind}")
        elif allowed.get(key, frozenset()) != values:
            allowed[key] = (allowed.get(key) or frozenset()) | {value}
    return allowed


def allowed_key(allowed: Allowed) -> Iterable[str]:
    """`allowed` as plain strings, for a cache key."""
    for key, values in allowed.items():
        yield from ([key] if values is None else (f"{key}={value}" for value in values))


def overpass_query(bbox: BoundingBox, allowed: Allowed) -> str:
    """One Overpass QL query for the named places of the allowed kinds in the box."""
    box = f"({bbox.south:.6f},{bbox.west:.6f},{bbox.north:.6f},{bbox.east:.6f})"
    public = '["access"!~"^(private|no|customers)$"]'
    lines = []
    for key, values in allowed.items():
        if values is None:
            lines += [f'  nwr["{key}"]["{tag}"]["name"]{public}{box};' for tag in BUILDING_NOTABLE]
        else:
            pattern = "|".join(sorted(values))
            lines.append(f'  nwr["{key}"~"^({pattern})$"]["name"]{public}{box};')
    return "[out:json][timeout:25];\n(\n" + "\n".join(lines) + "\n);\nout center tags;\n"


def parse_places(body: JsonValue, allowed: Allowed) -> list[Place]:
    """Overpass elements as places: allowed, public, named; merged and ranked, at most 200."""
    elements = body.get("elements") if isinstance(body, dict) else None
    places = []
    for element in elements if isinstance(elements, list) else []:
        place = to_place(element, allowed) if isinstance(element, dict) else None
        if place is not None:
            places.append(place)
    return rank(merge_duplicates(places))[:MAX_PLACES]


def to_place(element: Mapping[str, JsonValue], allowed: Allowed) -> Place | None:
    """An element as a place, or None if it isn't one a hunt may use."""
    raw = element.get("tags")
    if not isinstance(raw, dict):
        return None
    tags = {str(key): str(value) for key, value in raw.items()}
    name = tags.get("name", "").strip()
    kind = matched_kind(tags, allowed)
    location = element_location(element)
    if not name or kind is None or location is None or excluded(tags):
        return None
    return Place(
        osm=f"{element['type']}/{element['id']}",
        name=name,
        kind=kind,
        location=location,
        tags={k: v for k, v in tags.items() if k in TAGS_KEPT},
    )


def matched_kind(tags: Mapping[str, str], allowed: Allowed) -> str | None:
    """The first allowed tag the place has, e.g. `historic=memorial`."""
    for key, values in allowed.items():
        value = tags.get(key)
        if value is None:
            continue
        if values is None:
            if any(tag in tags for tag in BUILDING_NOTABLE):
                return f"{key}={value}"
        elif value in values:
            return f"{key}={value}"
    return None


def excluded(tags: Mapping[str, str]) -> bool:
    """Private, no access, customers only, or a place where children are."""
    if tags.get("access") in PRIVATE_ACCESS:
        return True
    return any(tags.get(key) in values for key, values in WITH_CHILDREN.items())


def element_location(element: Mapping[str, JsonValue]) -> Location | None:
    """A node's own point; a way's or relation's centre."""
    point = element if element.get("type") == "node" else element.get("center")
    if not isinstance(point, dict) or "lat" not in point or "lon" not in point:
        return None
    return Location(lat=float(str(point["lat"])), long=float(str(point["lon"])))


def documented(place: Place) -> bool:
    """Has a Wikipedia or Wikidata link, or an inscription: material for a good clue."""
    return any(tag in place.tags for tag in DOCUMENTED)


def merge_duplicates(places: Iterable[Place]) -> list[Place]:
    """One place per name within 30 m (a statue mapped as a node and as a way), the
    better-documented kept."""
    kept: list[Place] = []
    for place in places:
        twin = next(
            (
                i
                for i, other in enumerate(kept)
                if other.name.casefold() == place.name.casefold()
                and distance_m(other.location, place.location) < SAME_PLACE_M
            ),
            None,
        )
        if twin is None:
            kept.append(place)
        elif documented(place) and not documented(kept[twin]):
            kept[twin] = place
    return kept


def rank(places: Iterable[Place]) -> list[Place]:
    """Best-documented first, then by name and id, so the order is stable."""
    return sorted(places, key=lambda p: (not documented(p), p.name.casefold(), p.osm))


# --- the process-wide client ---------------------------------------------------------------


@lru_cache
def build_osm_client(
    nominatim_url: str, overpass_url: str, timeout_seconds: float, max_area_km: float
) -> OsmClient:
    """One client per configuration, so the rate limit and the cache are shared."""
    return OsmClient(
        Httpx2Client(),
        nominatim_url=nominatim_url,
        overpass_url=overpass_url,
        timeout_seconds=timeout_seconds,
        max_area_km=max_area_km,
    )


def osm_client(settings: Settings | None = None) -> OsmClient:
    """The process-wide client for the configured services."""
    settings = settings or get_settings()
    return build_osm_client(
        settings.osm_nominatim_url,
        settings.osm_overpass_url,
        settings.osm_timeout_seconds,
        settings.designer_max_area_km,
    )


def find_area(query: str) -> Area | None:
    """The area best matching `query`, clipped to the size limit; None if nothing matches."""
    return osm_client().find_area(query)


def find_places(area: Area, kinds: Sequence[str] | None = None) -> list[Place]:
    """The named, public places of the allowed kinds in the area, best-documented first."""
    return osm_client().find_places(area, kinds)

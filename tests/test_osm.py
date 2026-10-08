"""The OpenStreetMap client, against recorded and hand-made responses: no network."""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pytest
from pydantic import JsonValue

from game_server.config import Settings
from game_server.designer import osm
from game_server.designer.osm import (
    USER_AGENT,
    Area,
    HttpConnectionError,
    HttpReply,
    HttpTimeoutError,
    MapDataError,
    OsmClient,
    Place,
)
from game_server.drafts import BoundingBox
from game_server.geo import distance_m
from game_server.models import Location

FIXTURES = Path(__file__).parent / "fixtures" / "osm"
NOMINATIM = "https://nominatim.test"
OVERPASS = "https://overpass.test/api/interpreter"
TIMEOUT = 12.5


def fixture(name: str) -> JsonValue:
    body: JsonValue = json.loads((FIXTURES / name).read_text())
    return body


@dataclass(frozen=True)
class Call:
    method: str
    url: str
    params: Mapping[str, str] | None
    data: Mapping[str, str] | None
    headers: Mapping[str, str]
    timeout: float
    at: float


@dataclass
class FakeClock:
    """A monotonic clock that only moves when the client sleeps or a test advances it."""

    now: float = 1000.0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@dataclass
class FakeHttp:
    """Answers with queued replies, or raises queued errors; records every call."""

    clock: FakeClock
    replies: list[HttpReply | Exception] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)

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
        self.calls.append(Call(method, url, params, data, headers, timeout, self.clock.now))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def http(clock: FakeClock) -> FakeHttp:
    return FakeHttp(clock)


@pytest.fixture
def client(http: FakeHttp, clock: FakeClock) -> OsmClient:
    return OsmClient(
        http,
        nominatim_url=NOMINATIM,
        overpass_url=OVERPASS,
        timeout_seconds=TIMEOUT,
        max_area_km=3,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


def ok(body: JsonValue) -> HttpReply:
    return HttpReply(200, body)


SMALL = Area(
    name="Small",
    bbox=BoundingBox(south=51.490, west=-0.262, north=51.500, east=-0.250),
    centre=Location(lat=51.495, long=-0.256),
    clipped=False,
)


# --- an area -------------------------------------------------------------------------------


def test_an_area_is_found(client: OsmClient, http: FakeHttp) -> None:
    http.replies = [ok(fixture("nominatim_chiswick.json"))]

    area = client.find_area("Chiswick, London")

    assert area is not None
    assert area.name == "Chiswick, Greater London, England, W4 5PS, United Kingdom"
    assert area.centre == Location(lat=51.4923137, long=-0.2638180)
    [call] = http.calls
    assert (call.method, call.url) == ("GET", f"{NOMINATIM}/search")
    assert call.params == {"q": "Chiswick, London", "format": "jsonv2", "limit": "1"}


def test_nothing_found_is_none(client: OsmClient, http: FakeHttp) -> None:
    http.replies = [ok(fixture("nominatim_empty.json"))]

    assert client.find_area("Nowhere at all") is None


def test_a_large_area_is_clipped_around_its_centre(client: OsmClient, http: FakeHttp) -> None:
    http.replies = [ok(fixture("nominatim_chiswick.json"))]  # about 8.9 km by 5.5 km

    area = client.find_area("Chiswick, London")

    assert area is not None
    assert area.clipped is True
    box, centre = area.bbox, area.centre
    height = distance_m(
        Location(lat=box.south, long=centre.long), Location(lat=box.north, long=centre.long)
    )
    width = distance_m(
        Location(lat=centre.lat, long=box.west), Location(lat=centre.lat, long=box.east)
    )
    assert height == pytest.approx(3000, rel=0.01)
    assert width == pytest.approx(3000, rel=0.01)
    assert (box.south + box.north) / 2 == pytest.approx(centre.lat)
    assert (box.west + box.east) / 2 == pytest.approx(centre.long)


def test_a_small_area_is_not_clipped() -> None:
    body: JsonValue = [
        {
            "display_name": "Small",
            "lat": "51.495",
            "lon": "-0.256",
            "boundingbox": ["51.490", "51.500", "-0.262", "-0.250"],
        }
    ]

    assert osm.parse_area(body, max_area_km=3) == SMALL


def test_only_the_wide_direction_is_clipped() -> None:
    body: JsonValue = [  # about 1.1 km tall, 7 km wide
        {
            "display_name": "Long and thin",
            "lat": "51.495",
            "lon": "-0.256",
            "boundingbox": ["51.490", "51.500", "-0.306", "-0.206"],
        }
    ]

    area = osm.parse_area(body, max_area_km=3)

    assert area is not None
    assert area.clipped is True
    assert (area.bbox.south, area.bbox.north) == (51.490, 51.500)
    assert area.bbox.east - area.bbox.west < 0.1


def test_the_area_limit_is_a_setting() -> None:
    assert Settings().designer_max_area_km == 3
    assert osm.osm_client(Settings(designer_max_area_km=5)) is not osm.osm_client(Settings())


def test_an_area_as_a_draft_shows_it() -> None:
    assert SMALL.draft_area().model_dump() == {
        "name": "Small",
        "bbox": SMALL.bbox.model_dump(),
        "clipped": False,
    }


# --- the places ----------------------------------------------------------------------------


def places_from(client: OsmClient, http: FakeHttp, name: str) -> list[Place]:
    http.replies = [ok(fixture(name))]
    return client.find_places(SMALL)


def test_recorded_places_are_parsed(client: OsmClient, http: FakeHttp) -> None:
    places = places_from(client, http, "overpass_chiswick.json")

    recorded = fixture("overpass_chiswick.json")
    assert isinstance(recorded, dict)
    elements = recorded["elements"]
    assert isinstance(elements, list)
    assert 100 < len(places) <= len(elements)
    assert {p.osm.split("/")[0] for p in places} == {"node", "way", "relation"}
    for place in places:
        assert place.name
        assert set(place.tags) <= osm.TAGS_KEPT
        assert "=" in place.kind


def test_nodes_at_their_point_and_ways_and_relations_at_their_centre(
    client: OsmClient, http: FakeHttp
) -> None:
    places = {p.osm: p for p in places_from(client, http, "overpass_edge_cases.json")}

    assert places["node/1"].location == Location(lat=51.4901, long=-0.2601)
    assert places["way/2"].location == Location(lat=51.4911, long=-0.2611)
    assert places["relation/3"].location == Location(lat=51.4921, long=-0.2621)


def test_only_allowlisted_tags_are_kept(client: OsmClient, http: FakeHttp) -> None:
    places = {p.osm: p for p in places_from(client, http, "overpass_edge_cases.json")}

    assert places["node/1"].tags == {
        "historic": "memorial",
        "memorial": "statue",
        "name": "Kept Statue",
        "inscription": "To the brewers",
    }


def test_the_kind_is_the_tag_that_matched(client: OsmClient, http: FakeHttp) -> None:
    places = {p.osm: p for p in places_from(client, http, "overpass_edge_cases.json")}

    assert places["node/1"].kind == "historic=memorial"
    assert places["way/2"].kind == "leisure=park"
    assert places["relation/3"].kind == "building=church"


def test_excluded_places_are_left_out(client: OsmClient, http: FakeHttp) -> None:
    names = {p.name for p in places_from(client, http, "overpass_edge_cases.json")}

    for left_out in (
        "Private Sculpture",
        "No Access Monument",
        "Customers Garden",
        "Listed School",
        "Old Kindergarten",
        "Playground Sculpture",
        "Not A Kind Bakery",
        "No Centre Attraction",
        "Ordinary Building",
    ):
        assert left_out not in names
    assert "" not in names


def test_duplicates_are_merged_keeping_the_documented_one(
    client: OsmClient, http: FakeHttp
) -> None:
    places = [
        p
        for p in places_from(client, http, "overpass_edge_cases.json")
        if "mural" in p.name.lower()
    ]

    # Node 14 and way 15 are 11 m apart: one place, the one with a Wikipedia link.
    # Node 16 has the same name 440 m away: another place.
    assert sorted(p.osm for p in places) == ["node/16", "way/15"]


def test_best_documented_first(client: OsmClient, http: FakeHttp) -> None:
    places = places_from(client, http, "overpass_edge_cases.json")

    assert [p.osm for p in places] == ["relation/3", "node/1", "way/15", "way/2", "node/16"]


def artwork(n: int) -> JsonValue:
    tags: dict[str, JsonValue] = {"tourism": "artwork", "name": f"Artwork {n:03}"}
    if n % 2:
        tags["wikidata"] = f"Q{n}"
    return {"type": "node", "id": n, "lat": 51.0 + n * 0.001, "lon": -0.2, "tags": tags}


def test_at_most_two_hundred_places() -> None:
    places = osm.parse_places(
        {"elements": [artwork(n) for n in range(250)]}, osm.narrow_kinds(None)
    )

    assert len(places) == 200
    assert all(osm.documented(p) for p in places[:125])


def test_a_place_as_a_draft_shows_it(client: OsmClient, http: FakeHttp) -> None:
    [place, *_] = places_from(client, http, "overpass_edge_cases.json")

    assert place.draft_place().model_dump() == {
        "osm": "relation/3",
        "name": "Kept Listed Building",
        "kind": "building=church",
        "location": {"lat": 51.4921, "long": -0.2621},
    }


# --- the query -----------------------------------------------------------------------------


def query_for(client: OsmClient, http: FakeHttp, kinds: list[str] | None) -> str:
    http.replies = [ok({"elements": []})]
    client.find_places(SMALL, kinds)
    [call] = http.calls
    assert (call.method, call.url) == ("POST", OVERPASS)
    assert call.data is not None
    return call.data["data"]


def test_the_query_covers_the_box_and_every_kind(client: OsmClient, http: FakeHttp) -> None:
    query = query_for(client, http, None)

    assert query.startswith("[out:json][timeout:25];")
    assert query.rstrip().endswith("out center tags;")
    assert "(51.490000,-0.262000,51.500000,-0.250000)" in query
    for key in ("historic", "tourism", "amenity", "man_made", "leisure"):
        assert f'nwr["{key}"~' in query
    assert 'nwr["building"]["heritage"]["name"]' in query
    assert 'nwr["building"]["wikidata"]["name"]' in query
    assert query.count('["name"]') == 7
    assert query.count('["access"!~"^(private|no|customers)$"]') == 7


def test_kinds_narrow_the_query(client: OsmClient, http: FakeHttp) -> None:
    query = query_for(client, http, ["historic", "tourism=artwork"])

    assert 'nwr["historic"~"^(boundary_stone|building|city_gate|memorial|milestone' in query
    assert 'nwr["tourism"~"^(artwork)$"]' in query
    for absent in ("amenity", "man_made", "leisure", '"building"]', "museum"):
        assert absent not in query


def test_kinds_also_narrow_the_results(client: OsmClient, http: FakeHttp) -> None:
    http.replies = [ok(fixture("overpass_edge_cases.json"))]

    places = client.find_places(SMALL, ["leisure"])

    assert [p.osm for p in places] == ["way/2"]


@pytest.mark.parametrize(
    "kinds",
    [["shop"], ["tourism=hotel"], ["building=church"], ["amenity=school"]],
    ids=["unknown-key", "unknown-value", "building-value", "school"],
)
def test_kinds_outside_the_allowlist_are_refused(client: OsmClient, kinds: list[str]) -> None:
    with pytest.raises(ValueError, match="not an allowed kind"):
        client.find_places(SMALL, kinds)


# --- being a good citizen ------------------------------------------------------------------


def test_every_request_identifies_itself_and_has_a_timeout(
    client: OsmClient, http: FakeHttp
) -> None:
    http.replies = [ok(fixture("nominatim_chiswick.json")), ok({"elements": []})]

    client.find_area("Chiswick, London")
    client.find_places(SMALL)

    assert len(http.calls) == 2
    for call in http.calls:
        assert call.headers["User-Agent"] == USER_AGENT
        assert call.timeout == TIMEOUT
    assert USER_AGENT.startswith("scavenger-hunt-game-server/")
    assert USER_AGENT.endswith("(+https://github.com/ortaieb/scavenger-hunt-game-server)")


def test_nominatim_gets_at_most_one_request_a_second(
    client: OsmClient, http: FakeHttp, clock: FakeClock
) -> None:
    http.replies = [ok([]), ok([]), ok([])]

    client.find_area("First")
    clock.now += 0.25
    client.find_area("Second")
    clock.now += 5
    client.find_area("Third")

    first, second, third = (call.at for call in http.calls)
    assert second - first >= 1.0
    assert third - second >= 5  # no extra wait when a second has passed
    assert clock.sleeps == [pytest.approx(0.75)]


def test_overpass_is_not_held_to_nominatim_s_rate(
    client: OsmClient, http: FakeHttp, clock: FakeClock
) -> None:
    http.replies = [ok([]), ok({"elements": []})]

    client.find_area("First")
    client.find_places(SMALL)

    assert clock.sleeps == []


@pytest.mark.parametrize("status", [429, 504])
def test_a_retryable_answer_is_retried_once(
    client: OsmClient, http: FakeHttp, clock: FakeClock, status: int
) -> None:
    http.replies = [HttpReply(status, None), ok(fixture("nominatim_chiswick.json"))]

    area = client.find_area("Chiswick, London")

    assert area is not None
    assert len(http.calls) == 2
    assert http.calls[1].at - http.calls[0].at >= osm.RETRY_BACKOFF_SECONDS


def test_two_429s_are_map_unavailable(client: OsmClient, http: FakeHttp) -> None:
    http.replies = [HttpReply(429, None), HttpReply(429, None)]

    with pytest.raises(MapDataError) as raised:
        client.find_places(SMALL)

    assert raised.value.code == "map_unavailable"
    assert len(http.calls) == 2


@pytest.mark.parametrize("status", [400, 500, 503])
def test_other_failures_are_map_unavailable_without_a_retry(
    client: OsmClient, http: FakeHttp, status: int
) -> None:
    http.replies = [HttpReply(status, None)]

    with pytest.raises(MapDataError) as raised:
        client.find_places(SMALL)

    assert raised.value.code == "map_unavailable"
    assert len(http.calls) == 1


def test_a_timeout_is_map_timeout(client: OsmClient, http: FakeHttp) -> None:
    http.replies = [HttpTimeoutError("read timed out")]

    with pytest.raises(MapDataError) as raised:
        client.find_area("Chiswick, London")

    assert raised.value.code == "map_timeout"


def test_a_connection_error_is_map_unavailable(client: OsmClient, http: FakeHttp) -> None:
    http.replies = [HttpConnectionError("connection refused")]

    with pytest.raises(MapDataError) as raised:
        client.find_places(SMALL)

    assert raised.value.code == "map_unavailable"


# --- caching -------------------------------------------------------------------------------


def test_a_cached_area_makes_no_request(client: OsmClient, http: FakeHttp) -> None:
    http.replies = [ok(fixture("nominatim_chiswick.json"))]

    first = client.find_area("Chiswick, London")
    again = client.find_area("  chiswick,   LONDON ")

    assert again == first
    assert len(http.calls) == 1


def test_a_cached_miss_makes_no_request(client: OsmClient, http: FakeHttp) -> None:
    http.replies = [ok([])]

    assert client.find_area("Nowhere") is None
    assert client.find_area("Nowhere") is None
    assert len(http.calls) == 1


def test_cached_places_make_no_request(client: OsmClient, http: FakeHttp) -> None:
    http.replies = [ok(fixture("overpass_edge_cases.json"))]

    first = client.find_places(SMALL, ["historic", "leisure"])
    again = client.find_places(SMALL, ["leisure", "historic"])

    assert again == first
    assert len(http.calls) == 1


def test_other_kinds_or_boxes_are_cached_apart(client: OsmClient, http: FakeHttp) -> None:
    http.replies = [ok({"elements": []}) for _ in range(3)]
    other = SMALL.model_copy(update={"bbox": SMALL.bbox.model_copy(update={"south": 51.489})})

    client.find_places(SMALL)
    client.find_places(SMALL, ["leisure"])
    client.find_places(other)

    assert len(http.calls) == 3


def test_the_cache_lasts_an_hour(client: OsmClient, http: FakeHttp, clock: FakeClock) -> None:
    http.replies = [ok([]), ok([]), ok([])]

    client.find_area("Nowhere")
    clock.now += 3599
    client.find_area("Nowhere")
    clock.now += 2
    client.find_area("Nowhere")

    assert len(http.calls) == 2


def test_a_failure_is_not_cached(client: OsmClient, http: FakeHttp) -> None:
    http.replies = [HttpReply(500, None), ok([])]

    with pytest.raises(MapDataError):
        client.find_area("Nowhere")
    client.find_area("Nowhere")

    assert len(http.calls) == 2


# --- logging -------------------------------------------------------------------------------


def test_one_line_per_request_without_the_query(
    client: OsmClient, http: FakeHttp, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    http.replies = [
        HttpReply(429, None),
        ok(fixture("nominatim_chiswick.json")),
        ok(fixture("overpass_edge_cases.json")),
    ]

    client.find_area("Chiswick, London")
    client.find_places(SMALL)

    lines = [r.getMessage() for r in caplog.records if r.name == "game_server.designer.osm"]
    assert lines == [
        "Map request nominatim status 429 results - duration_ms 0",
        "Map request nominatim status 200 results 1 duration_ms 0",
        "Map request overpass status 200 results 17 duration_ms 0",
    ]
    assert "Chiswick" not in caplog.text
    assert "51.49" not in caplog.text


# --- the real services ---------------------------------------------------------------------


@pytest.mark.live
def test_live_a_london_area_and_its_places() -> None:
    """Calls the real Nominatim and Overpass, once each."""
    client = osm.build_osm_client(
        "https://nominatim.openstreetmap.org", "https://overpass-api.de/api/interpreter", 60, 1.5
    )

    area = client.find_area("Chiswick, London")
    assert area is not None
    places = client.find_places(area, ["historic", "tourism=artwork"])

    assert area.clipped is True
    assert places
    assert all(p.kind.startswith(("historic=", "tourism=artwork")) for p in places)

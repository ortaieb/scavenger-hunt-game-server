"""A checkpoint's reference photos: prepared once, capped, and sent before the player's photo."""

import base64
import hashlib
import json
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import BytesIO
from itertools import count
from pathlib import Path
from threading import Barrier
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import anthropic
import psycopg
import pytest
from fastapi.testclient import TestClient
from images import GPS_IFD, jpeg, photo_with_metadata, scene
from PIL import Image
from psycopg.rows import DictRow
from pytest_mock import MockerFixture

from game_server import referee_references
from game_server.app import create_app
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.referee import ClaudeReferee, ModelReply, get_referee, prepare_image, user_text
from game_server.referee_references import (
    ReferencePhotoError,
    ReferencePhotos,
    get_reference_photos,
    prepare_references,
)
from game_server.sessions import (
    SessionRepository,
    VisualChallenge,
    get_session_repository,
    parse_sessions,
)

SESSION = "aeffe667-4f9f-4108-b5e2-56ae821fe413"
MODERATOR = "MOD-8H3T-QX"
FOX, HERON, OWL = "FOX-7Q2K", "HERON-4MXP", "OWL-3JD8"
NOW = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
LAT, LONG = 51.5034, -0.1276
# A file name can describe the place: it must never reach a request, a trace or a log line.
NAMES = ("SENTINEL-fountain-north", "SENTINEL-fountain-south", "SENTINEL-fountain-east")
PATHS = tuple(f"reference/{name}.jpg" for name in NAMES)
PHOTOS = count(700)  # distinct seeds: no photo is a duplicate of another
WRAPPER = "game_server.referee._create_structured_message"
MODEL_OUTPUT = json.dumps(
    {
        "scene_matches": {"reason": "The fountain.", "verdict": "pass", "confidence": 0.9},
        "pose_correct": {"reason": "A wave.", "verdict": "pass", "confidence": 0.9},
    }
)
Db = psycopg.Connection[DictRow]


def write_references(directory: Path) -> list[Path]:
    """Large photos carrying GPS in their EXIF; the second is stored rotated."""
    (directory / "reference").mkdir(exist_ok=True)
    photos = [
        photo_with_metadata(scene(40, (1200, 900))),
        photo_with_metadata(scene(41, (1200, 900)).rotate(90, expand=True), orientation=6),
        photo_with_metadata(scene(42, (1200, 900))),
    ]
    paths = [directory / path for path in PATHS]
    for path, photo in zip(paths, photos, strict=True):
        path.write_bytes(photo)
    return paths


def sessions_json() -> str:
    """Checkpoint 1 lists every reference photo; checkpoint 2 has none."""
    checkpoint = {"location": {"lat": LAT, "long": LONG}, "proximity": 40}
    return json.dumps(
        [
            {
                "id": SESSION,
                "name": "Hunt",
                "location": "Here",
                "start-time": "2026-10-03T09:00:00Z",
                "end-time": "2026-10-03T12:00:00Z",
                "moderator-code": MODERATOR,
                "checkpoints": [
                    {
                        **checkpoint,
                        "sequence": 1,
                        "name": "Fountain",
                        "clue": "Water",
                        "challenge": {"scene": "A stone fountain", "pose": "Wave"},
                        "reference-photos": list(PATHS),
                    },
                    {
                        **checkpoint,
                        "sequence": 2,
                        "name": "Statue",
                        "clue": "Bronze",
                        "challenge": {"scene": "A bronze statue", "pose": "Point"},
                    },
                ],
                "teams": [
                    {"name": "Red Foxes", "join-code": FOX, "order": [1, 2]},
                    {"name": "Blue Herons", "join-code": HERON, "order": [1, 2]},
                    {"name": "Grey Owls", "join-code": OWL, "order": [2, 1]},
                ],
            }
        ]
    )


@pytest.fixture
def paths(tmp_path: Path) -> list[Path]:
    return write_references(tmp_path)


@pytest.fixture
def sessions(tmp_path: Path, paths: list[Path]) -> SessionRepository:
    return parse_sessions(sessions_json(), reference_dir=tmp_path)


def decoded(jpeg_bytes: bytes) -> Image.Image:
    return Image.open(BytesIO(jpeg_bytes))


# --- preparing ---------------------------------------------------------------------------


def test_prepares_the_first_max_references_in_file_order(paths: list[Path]) -> None:
    prepared = prepare_references(paths, 2, 768)

    assert [reference.position for reference in prepared] == [0, 1]
    assert prepared[0].image == prepare_image(paths[0].read_bytes(), 768)


def test_photos_past_the_cap_are_never_read(tmp_path: Path) -> None:
    present = write_references(tmp_path)[:1]

    prepared = prepare_references([*present, tmp_path / "missing.jpg"], 1, 768)

    assert len(prepared) == 1


def test_zero_references_reads_nothing(tmp_path: Path) -> None:
    assert prepare_references([tmp_path / "missing.jpg"], 0, 768) == ()


@pytest.mark.parametrize("max_edge", [768, 320])
def test_prepared_references_are_upright_small_and_carry_no_exif(
    paths: list[Path], max_edge: int
) -> None:
    assert decoded(paths[0].read_bytes()).getexif().get_ifd(GPS_IFD)  # the input has GPS

    for reference in prepare_references(paths, 3, max_edge):
        image = decoded(reference.image.jpeg)
        assert max(image.size) <= max_edge
        assert image.width > image.height  # all three are landscape once upright
        assert (image.width, image.height) == (reference.image.width, reference.image.height)
        assert dict(image.getexif()) == {}
        assert b"PhoneMaker" not in reference.image.jpeg


def test_an_unreadable_photo_is_named_by_position_never_by_path(paths: list[Path]) -> None:
    paths[1].unlink()

    with pytest.raises(ReferencePhotoError) as excinfo:
        prepare_references(paths, 2, 768)

    assert str(excinfo.value) == "reference-photos[1]: can't be read (FileNotFoundError)"
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__  # the OSError, path and all, isn't chained


def test_an_undecodable_photo_is_named_by_position(paths: list[Path]) -> None:
    paths[0].write_bytes(b"\xff\xd8\xffjunk")

    with pytest.raises(ReferencePhotoError, match=r"^reference-photos\[0\]: doesn't decode$"):
        prepare_references(paths, 2, 768)


# --- once per checkpoint -------------------------------------------------------------------


def test_a_checkpoint_is_prepared_once(
    sessions: SessionRepository, paths: list[Path], mocker: MockerFixture
) -> None:
    spy = mocker.spy(referee_references, "prepare_references")
    photos = ReferencePhotos(sessions, 2, 768)

    first = photos.for_checkpoint(UUID(SESSION), 1)
    for path in paths:
        path.unlink()  # a second preparation would fail
    second = photos.for_checkpoint(UUID(SESSION), 1)

    assert len(first) == 2
    assert second is first
    spy.assert_called_once()


def test_concurrent_first_judgements_prepare_once(
    sessions: SessionRepository, mocker: MockerFixture
) -> None:
    spy = mocker.spy(referee_references, "prepare_references")
    photos = ReferencePhotos(sessions, 2, 768)
    barrier = Barrier(4)

    def first_use() -> object:
        barrier.wait()
        return photos.for_checkpoint(UUID(SESSION), 1)

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: first_use(), range(4)))

    assert all(result is results[0] for result in results)
    spy.assert_called_once()


def test_a_checkpoint_without_references_gets_none(sessions: SessionRepository) -> None:
    assert ReferencePhotos(sessions, 2, 768).for_checkpoint(UUID(SESSION), 2) == ()


def test_a_broken_photo_sends_none_and_warns_once_by_position(
    sessions: SessionRepository, paths: list[Path], caplog: pytest.LogCaptureFixture
) -> None:
    paths[1].write_bytes(b"\xff\xd8\xffjunk")  # broken after the startup check
    caplog.set_level(logging.WARNING, logger="game_server.referee_references")
    photos = ReferencePhotos(sessions, 2, 768)

    assert photos.for_checkpoint(UUID(SESSION), 1) == ()
    assert photos.for_checkpoint(UUID(SESSION), 1) == ()

    [warning] = caplog.records
    assert warning.getMessage() == (
        f"referee: session {SESSION} checkpoint 1 reference-photos[1]: doesn't decode; "
        "sending no reference photos"
    )
    assert not any(name in caplog.text for name in NAMES)


def test_reference_photos_follow_the_settings(sessions: SessionRepository) -> None:
    settings = Settings(referee_max_references=1, referee_reference_max_edge=320)

    photos = get_reference_photos(settings, sessions)

    assert (photos.max_references, photos.max_edge) == (1, 320)
    assert get_reference_photos(settings, sessions) is photos  # built once per configuration


# --- through POST /challenge ------------------------------------------------------------------


@pytest.fixture
def max_references() -> int:
    return 2


@pytest.fixture
def wrapper(mocker: MockerFixture) -> MagicMock:
    """The SDK call, faked: the real referee builds the request."""
    reply = ModelReply("end_turn", MODEL_OUTPUT, "claude-haiku-4-5", "req_refs", 2500, 120)
    mock: MagicMock = mocker.patch(WRAPPER, return_value=reply)
    return mock


@pytest.fixture
def client(
    tmp_path: Path, sessions: SessionRepository, wrapper: MagicMock, max_references: int
) -> Iterator[TestClient]:
    app = create_app()
    sdk = anthropic.Anthropic(api_key="test-key-not-used")
    referee = ClaudeReferee(sdk, "claude-haiku-4-5", max_image_edge=1568)
    settings = Settings(
        image_base_path=tmp_path / "images",
        referee_max_references=max_references,
        referee_reference_max_edge=320,
    )
    app.dependency_overrides[get_referee] = lambda: referee
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: sessions
    app.dependency_overrides[get_clock] = lambda: lambda: NOW
    with TestClient(app) as test_client:
        moderator = {"Authorization": f"Bearer {MODERATOR}"}
        assert test_client.post(f"/sessions/{SESSION}/start", headers=moderator).status_code == 201
        yield test_client


def submit(client: TestClient, wrapper: MagicMock, code: str, checkpoint: int) -> list[Any]:
    """Join, check in and send a photo; returns the user turn the referee sent with it."""
    participant = client.post("/join", json={"code": code, "consent": True}).json()["participant"]
    arrived = client.post(
        f"/sessions/{SESSION}/participants/{participant}/arrive", json={"checkpoint": checkpoint}
    )
    assert arrived.status_code == 201, arrived.text
    metadata = {
        "session": SESSION,
        "participant": participant,
        "checkpoint": checkpoint,
        "location": {"lat": LAT, "long": LONG},
        "capture-time": NOW.isoformat(),
    }
    photo = jpeg(scene(next(PHOTOS)))
    response = client.post(
        "/challenge",
        files={
            "metadata": (None, json.dumps(metadata), "application/json"),
            "challenge-image": ("p.jpeg", photo, "image/jpeg"),
        },
    )
    assert response.status_code == 200, response.text  # judged: a final verdict
    content: list[Any] = wrapper.call_args.kwargs["content"]
    assert image_of(content[-2]) == prepare_image(photo, 1568).jpeg  # the player's photo
    return content


def image_of(block: dict[str, Any]) -> bytes:
    assert block["type"] == "image"
    assert block["source"]["media_type"] == "image/jpeg"
    return base64.b64decode(block["source"]["data"])


def traces(db: Db) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute("SELECT * FROM referee_traces ORDER BY id")]


def test_labelled_references_come_before_the_players_photo(
    client: TestClient, wrapper: MagicMock
) -> None:
    content = submit(client, wrapper, FOX, 1)

    assert [block["text"] for block in content[0:6:2]] == [
        "Reference photo 1 of 2: the checkpoint, photographed by the organiser",
        "Reference photo 2 of 2: the checkpoint, photographed by the organiser",
        "The player's photo",
    ]
    assert content[6]["text"] == user_text(
        VisualChallenge(scene="A stone fountain", pose="Wave"), with_references=True
    )
    assert len(content) == 7  # no more than GAME_SERVER_REFEREE_MAX_REFERENCES


def test_sent_references_are_small_upright_and_carry_no_exif(
    client: TestClient, wrapper: MagicMock
) -> None:
    content = submit(client, wrapper, FOX, 1)

    for block in content[1:4:2]:
        sent = image_of(block)
        image = decoded(sent)
        assert max(image.size) <= 320  # GAME_SERVER_REFEREE_REFERENCE_MAX_EDGE
        assert image.width > image.height  # upright, the rotated one included
        assert dict(image.getexif()) == {}
        assert b"PhoneMaker" not in sent


def test_the_trace_records_the_references_by_position_and_hash(
    client: TestClient, wrapper: MagicMock, db: Db
) -> None:
    content = submit(client, wrapper, FOX, 1)

    [trace] = traces(db)
    assert trace["reference_photos"] == [
        {"position": 0, "sha256": hashlib.sha256(image_of(content[1])).hexdigest()},
        {"position": 1, "sha256": hashlib.sha256(image_of(content[3])).hexdigest()},
    ]
    moderator = {"Authorization": f"Bearer {MODERATOR}"}
    shown = client.get(f"/sessions/{SESSION}/traces", headers=moderator).json()
    assert shown["items"][0]["trace"]["references"] == trace["reference_photos"]
    for name in NAMES:  # never the path
        assert name not in json.dumps(trace, default=str)
        assert name not in json.dumps(shown)


def test_a_second_judgement_does_not_read_the_files_again(
    client: TestClient, wrapper: MagicMock, paths: list[Path], db: Db
) -> None:
    first = submit(client, wrapper, FOX, 1)
    for path in paths:
        path.unlink()

    second = submit(client, wrapper, HERON, 1)

    assert second[:5] == first[:5]  # the same labelled references, from memory
    first_trace, second_trace = traces(db)
    assert len(second_trace["reference_photos"]) == 2
    assert second_trace["reference_photos"] == first_trace["reference_photos"]


def test_a_checkpoint_without_references_is_judged_as_before(
    client: TestClient, wrapper: MagicMock, db: Db
) -> None:
    content = submit(client, wrapper, OWL, 2)

    _, text_block = content  # the player's photo, then the text: as before
    assert text_block["text"] == user_text(VisualChallenge(scene="A bronze statue", pose="Point"))
    [trace] = traces(db)
    assert trace["reference_photos"] == []


@pytest.mark.parametrize("max_references", [0])
def test_max_references_zero_judges_as_before(
    client: TestClient, wrapper: MagicMock, db: Db
) -> None:
    content = submit(client, wrapper, FOX, 1)

    _, text_block = content  # the player's photo, then the text: as before
    assert text_block["text"] == user_text(VisualChallenge(scene="A stone fountain", pose="Wave"))
    [trace] = traces(db)
    assert trace["reference_photos"] == []

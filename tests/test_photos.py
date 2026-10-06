"""The moderator's photo endpoints: `GET …/submissions/{submission}/photo` and
`GET …/checkpoints/{sequence}/reference-photos/{position}`."""

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from images import EXIF_ORIENTATION, GPS_IFD, jpeg, photo_with_metadata, scene
from PIL import Image

from game_server.app import create_app
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.referee import prepare_image
from game_server.sessions import get_session_repository, parse_sessions
from game_server.storage import ImageStore
from game_server.submissions import NewSubmission, SubmissionStore

SESSION = "aeffe667-4f9f-4108-b5e2-56ae821fe413"
OTHER = "0b5e9c1e-2f7a-4d8e-9a57-3c1f6f0d2b44"
MODERATOR = "MOD-8H3T-QX"
OTHER_MODERATOR = "MOD-2KV9-ZP"
FOX = "FOX-7Q2K"
START = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
DURING = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
LAT, LONG = 51.5034, -0.1276
MAX_EDGE = 120
# A reference photo's file name can describe the place: never in a response or a log line.
PLACE = "SENTINEL-PLACE-north-gate"
REFERENCES = (f"{PLACE}-0.jpg", f"{PLACE}-1.jpg", f"{PLACE}-2.jpg")


def session_json(session_id: str, moderator: str, suffix: str = "") -> dict[str, Any]:
    return {
        "id": session_id,
        "name": "Hunt",
        "location": "Here",
        "start-time": START.isoformat(),
        "end-time": (START + timedelta(hours=3)).isoformat(),
        "moderator-code": moderator,
        "checkpoints": [
            {
                "sequence": 1,
                "name": "Lion fountain",
                "clue": "Clue 1",
                "location": {"lat": LAT, "long": LONG},
                "proximity": 40,
                "reference-photos": [f"reference/{name}" for name in REFERENCES],
            },
            {
                "sequence": 2,
                "name": "Bandstand",
                "clue": "Clue 2",
                "location": {"lat": LAT, "long": LONG},
                "proximity": 40,
            },
        ],
        "teams": [{"name": "Foxes", "join-code": f"{FOX}{suffix}", "order": [1, 2]}],
    }


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(image_base_path=tmp_path / "images", referee_max_image_edge=MAX_EDGE)


@pytest.fixture
def reference_dir(tmp_path: Path) -> Path:
    """The sessions file's directory, holding checkpoint 1's three reference photos."""
    (tmp_path / "reference").mkdir()
    for seed, name in enumerate(REFERENCES):
        reference = scene(seed, (400 + 40 * seed, 300))
        (tmp_path / "reference" / name).write_bytes(photo_with_metadata(reference))
    return tmp_path


@pytest.fixture
def client(settings: Settings, reference_dir: Path) -> Iterator[TestClient]:
    app = create_app()
    sessions = parse_sessions(
        json.dumps(
            [session_json(SESSION, MODERATOR), session_json(OTHER, OTHER_MODERATOR, "-OTHER")]
        ),
        reference_dir=reference_dir,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: sessions
    app.dependency_overrides[get_clock] = lambda: lambda: DURING
    with TestClient(app) as test_client:
        yield test_client


def store_photo(
    store: SubmissionStore, settings: Settings, photo: bytes, session: str = SESSION
) -> tuple[int, Path]:
    """Save the photo as `POST /challenge` does and record its submission; returns its id and
    file."""
    image_id, path = ImageStore(settings.image_base_path).save(photo)
    submission = store.record(
        NewSubmission(
            session=UUID(session),
            participant=uuid4(),
            checkpoint=1,
            received_at=DURING,
            capture_time=DURING,
            lat=LAT,
            long=LONG,
            image_id=image_id,
            verdict="pending",
            checks=(),
            distance_m=1.0,
            phash=0,
            processing_ms=40,
        )
    ).id
    return submission, path


def get(client: TestClient, path: str, authorization: str | None = MODERATOR) -> Response:
    headers = {"Authorization": f"Bearer {authorization}"} if authorization else {}
    return client.get(f"/sessions/{SESSION}/{path}", headers=headers)


def opened(response: Response) -> Image.Image:
    return Image.open(BytesIO(response.content))


# A landscape photo (600 x 400), stored rotated with an EXIF orientation as phones save it.
LANDSCAPE = scene(11, (600, 400))
STORED_ROTATED = photo_with_metadata(LANDSCAPE.rotate(90, expand=True), orientation=6)


@pytest.fixture
def served(client: TestClient, store: SubmissionStore, settings: Settings) -> Response:
    submission, _ = store_photo(store, settings, STORED_ROTATED)
    return get(client, f"submissions/{submission}/photo")


# --- the player's photo ----------------------------------------------------------------------


def test_the_photo_is_a_jpeg_as_the_referee_saw_it(served: Response) -> None:
    assert served.status_code == 200
    assert served.headers["Content-Type"] == "image/jpeg"
    assert served.content == prepare_image(STORED_ROTATED, MAX_EDGE).jpeg


def test_the_photo_is_never_cached(served: Response) -> None:
    assert served.headers["Cache-Control"] == "no-store"


def test_the_photos_long_edge_is_at_most_the_setting(served: Response) -> None:
    assert max(opened(served).size) == MAX_EDGE


def test_an_exif_rotated_photo_is_upright(served: Response) -> None:
    width, height = opened(served).size

    assert width > height  # stored as a portrait, shot as a landscape


def test_the_photo_has_no_exif(served: Response) -> None:
    image = opened(served)

    assert "exif" not in image.info
    assert EXIF_ORIENTATION not in image.getexif()
    assert not image.getexif().get_ifd(GPS_IFD)


def test_a_small_photo_is_not_enlarged(
    client: TestClient, store: SubmissionStore, settings: Settings
) -> None:
    submission, _ = store_photo(store, settings, jpeg(scene(12, (80, 60))))

    assert opened(get(client, f"submissions/{submission}/photo")).size == (80, 60)


def test_each_photo_served_is_logged_without_its_path(
    client: TestClient,
    store: SubmissionStore,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    submission, path = store_photo(store, settings, STORED_ROTATED)

    get(client, f"submissions/{submission}/photo")

    served = [r for r in caplog.records if r.name == "game_server.photos"]
    assert [r.getMessage() for r in served] == [
        f"Moderator photo served: session {SESSION} submission {submission}"
    ]
    assert path.stem not in caplog.text
    assert str(settings.image_base_path) not in caplog.text


def test_an_unknown_submission_is_404(
    client: TestClient, store: SubmissionStore, settings: Settings
) -> None:
    submission, _ = store_photo(store, settings, STORED_ROTATED)

    response = get(client, f"submissions/{submission + 1}/photo")

    assert (response.status_code, response.json()) == (404, {"detail": "unknown submission"})


def test_another_sessions_photo_is_404(
    client: TestClient, store: SubmissionStore, settings: Settings
) -> None:
    elsewhere, _ = store_photo(store, settings, STORED_ROTATED, session=OTHER)

    response = get(client, f"submissions/{elsewhere}/photo")

    assert (response.status_code, response.json()) == (404, {"detail": "unknown submission"})
    assert (
        client.get(
            f"/sessions/{OTHER}/submissions/{elsewhere}/photo",
            headers={"Authorization": f"Bearer {OTHER_MODERATOR}"},
        ).status_code
        == 200
    )


def test_a_missing_file_is_404(
    client: TestClient,
    store: SubmissionStore,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    submission, path = store_photo(store, settings, STORED_ROTATED)
    path.unlink()

    response = get(client, f"submissions/{submission}/photo")

    assert (response.status_code, response.json()) == (404, {"detail": "photo not found"})
    assert path.stem not in response.text
    assert path.stem not in caplog.text


@pytest.mark.parametrize("submission", ["0", "-1", str(2**63), "abc"])
def test_a_malformed_submission_id_is_422(client: TestClient, submission: str) -> None:
    assert get(client, f"submissions/{submission}/photo").status_code == 422


# --- reference photos ------------------------------------------------------------------------


@pytest.mark.parametrize("position", range(len(REFERENCES)))
def test_each_reference_photo_as_the_referee_would_see_it(
    client: TestClient, reference_dir: Path, position: int
) -> None:
    original = (reference_dir / "reference" / REFERENCES[position]).read_bytes()

    response = get(client, f"checkpoints/1/reference-photos/{position}")

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "image/jpeg"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.content == prepare_image(original, MAX_EDGE).jpeg


def test_a_reference_photo_has_no_exif_and_fits_the_setting(client: TestClient) -> None:
    image = opened(get(client, "checkpoints/1/reference-photos/0"))

    assert max(image.size) == MAX_EDGE
    assert "exif" not in image.info
    assert not image.getexif().get_ifd(GPS_IFD)


def test_past_the_last_reference_photo_is_404(client: TestClient) -> None:
    response = get(client, f"checkpoints/1/reference-photos/{len(REFERENCES)}")

    assert (response.status_code, response.json()) == (404, {"detail": "unknown reference photo"})


def test_a_checkpoint_without_reference_photos_is_404(client: TestClient) -> None:
    response = get(client, "checkpoints/2/reference-photos/0")

    assert (response.status_code, response.json()) == (404, {"detail": "unknown reference photo"})


def test_an_unknown_checkpoint_is_404(client: TestClient) -> None:
    response = get(client, "checkpoints/9/reference-photos/0")

    assert (response.status_code, response.json()) == (404, {"detail": "unknown checkpoint"})


def test_a_reference_photo_gone_from_disk_is_404(client: TestClient, reference_dir: Path) -> None:
    (reference_dir / "reference" / REFERENCES[1]).unlink()

    response = get(client, "checkpoints/1/reference-photos/1")

    assert (response.status_code, response.json()) == (404, {"detail": "photo not found"})


@pytest.mark.parametrize(
    "path", ["checkpoints/0/reference-photos/0", "checkpoints/1/reference-photos/-1"]
)
def test_a_malformed_checkpoint_or_position_is_422(client: TestClient, path: str) -> None:
    assert get(client, path).status_code == 422


def test_each_reference_photo_served_is_logged_by_position(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    response = get(client, "checkpoints/1/reference-photos/2")

    served = [r for r in caplog.records if r.name == "game_server.photos"]
    assert [r.getMessage() for r in served] == [
        f"Moderator reference photo served: session {SESSION} checkpoint 1 position 2"
    ]
    assert PLACE not in caplog.text
    assert PLACE.encode() not in response.content


# --- authorisation ---------------------------------------------------------------------------

PHOTO_PATHS = ["submissions/{submission}/photo", "checkpoints/1/reference-photos/0"]


@pytest.mark.parametrize("path", PHOTO_PATHS, ids=["photo", "reference-photo"])
@pytest.mark.parametrize(
    "authorization",
    [None, "MOD-WRONG-1", OTHER_MODERATOR, FOX],
    ids=["no-code", "wrong", "other-session", "join-code"],
)
def test_moderator_code_required(
    client: TestClient,
    store: SubmissionStore,
    settings: Settings,
    path: str,
    authorization: str | None,
) -> None:
    submission, _ = store_photo(store, settings, STORED_ROTATED)

    response = get(client, path.format(submission=submission), authorization)

    assert response.status_code == 401
    assert response.json() == {
        "detail": "moderator code required",
        "code": "moderator_unauthorised",
    }
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("path", PHOTO_PATHS, ids=["photo", "reference-photo"])
def test_unknown_session_is_404(
    client: TestClient, store: SubmissionStore, settings: Settings, path: str
) -> None:
    submission, _ = store_photo(store, settings, STORED_ROTATED)

    response = client.get(
        f"/sessions/{uuid4()}/{path.format(submission=submission)}",
        headers={"Authorization": f"Bearer {MODERATOR}"},
    )

    assert (response.status_code, response.json()) == (404, {"detail": "unknown session"})

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from game_server.app import create_app
from game_server.config import Settings, get_settings

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00fake-jpeg-body\xff\xd9"
MAX_IMAGE_BYTES = 1024

METADATA: dict[str, Any] = {
    "session": "aeffe667-4f9f-4108-b5e2-56ae821fe413",
    "participant": "7c860ccc-9adf-4e22-b54f-3ff158f5d600",
    "location": {"lat": 51.509948, "long": -1.485923},
    "capture-time": "2012-03-29T10:05:45-06:00",
}


@pytest.fixture
def image_dir(tmp_path: Path) -> Path:
    return tmp_path / "images"


@pytest.fixture
def client(image_dir: Path) -> Iterator[TestClient]:
    app = create_app()
    settings = Settings(image_base_path=image_dir, max_image_bytes=MAX_IMAGE_BYTES)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as test_client:
        yield test_client


def post_challenge(
    client: TestClient,
    metadata: str = json.dumps(METADATA),
    image: bytes = JPEG,
    image_type: str = "image/jpeg",
) -> Response:
    return client.post(
        "/challenge",
        files={
            "metadata": (None, metadata, "application/json"),
            "challenge-image": ("photo.jpeg", image, image_type),
        },
    )


def stored_files(image_dir: Path) -> list[Path]:
    return sorted(image_dir.glob("*")) if image_dir.exists() else []


def test_accepts_valid_challenge(client: TestClient) -> None:
    response = post_challenge(client)

    assert response.status_code == 202
    UUID(response.json()["image_id"])


def test_stores_image_under_base_path_named_by_image_id(
    client: TestClient, image_dir: Path
) -> None:
    image_id = post_challenge(client).json()["image_id"]

    stored = image_dir.resolve() / f"{image_id}.jpeg"
    assert stored_files(image_dir) == [stored]
    assert stored.read_bytes() == JPEG


def test_logs_received_challenge(
    client: TestClient, image_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    logging.getLogger("game_server").addHandler(caplog.handler)
    try:
        image_id = post_challenge(client).json()["image_id"]
    finally:
        logging.getLogger("game_server").removeHandler(caplog.handler)

    assert caplog.messages == [
        "Received challenge request for "
        "aeffe667-4f9f-4108-b5e2-56ae821fe413[7c860ccc-9adf-4e22-b54f-3ff158f5d600] "
        "arrived at 2012-03-29T10:05:45-06:00 from (51.509948,-1.485923), "
        f"image stored in: {image_dir.resolve() / f'{image_id}.jpeg'}"
    ]


@pytest.mark.parametrize(
    "metadata",
    [
        "not json",
        json.dumps({**METADATA, "session": "not-a-uuid"}),
        json.dumps({k: v for k, v in METADATA.items() if k != "capture-time"}),
    ],
)
def test_rejects_invalid_metadata_without_storing(
    client: TestClient, image_dir: Path, metadata: str
) -> None:
    response = post_challenge(client, metadata=metadata)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][:2] == ["body", "metadata"]
    assert stored_files(image_dir) == []


def test_rejects_non_jpeg_content_type(client: TestClient, image_dir: Path) -> None:
    response = post_challenge(client, image_type="image/png")

    assert response.status_code == 415
    assert stored_files(image_dir) == []


@pytest.mark.parametrize("image", [b"", b"\x89PNG\r\n\x1a\n-not-a-jpeg"])
def test_rejects_body_that_is_not_jpeg(client: TestClient, image_dir: Path, image: bytes) -> None:
    response = post_challenge(client, image=image)

    assert response.status_code == 422
    assert stored_files(image_dir) == []


def test_accepts_image_at_size_limit(client: TestClient) -> None:
    image = JPEG + b"\x00" * (MAX_IMAGE_BYTES - len(JPEG))

    assert post_challenge(client, image=image).status_code == 202


def test_rejects_image_over_size_limit(client: TestClient, image_dir: Path) -> None:
    image = JPEG + b"\x00" * (MAX_IMAGE_BYTES - len(JPEG) + 1)

    response = post_challenge(client, image=image)

    assert response.status_code == 413
    assert stored_files(image_dir) == []


@pytest.mark.parametrize("missing", ["metadata", "challenge-image"])
def test_rejects_missing_part(client: TestClient, missing: str) -> None:
    files: dict[str, tuple[str | None, bytes | str, str]] = {
        "metadata": (None, json.dumps(METADATA), "application/json"),
        "challenge-image": ("photo.jpeg", JPEG, "image/jpeg"),
    }
    del files[missing]

    response = client.post("/challenge", files=files)

    assert response.status_code == 422

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from game_server.app import create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as test_client:
        yield test_client


def test_root_returns_hello_world(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.text == "Hello, World!"


def test_root_is_plain_text(client: TestClient) -> None:
    response = client.get("/")

    assert response.headers["content-type"].startswith("text/plain")


def test_unknown_path_returns_404(client: TestClient) -> None:
    response = client.get("/does-not-exist")

    assert response.status_code == 404

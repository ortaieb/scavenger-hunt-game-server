from pathlib import Path

import pytest

from game_server.storage import ImageStore


def test_save_writes_bytes_to_uuid_named_jpeg(tmp_path: Path) -> None:
    store = ImageStore(tmp_path)

    image_id, path = store.save(b"\xff\xd8\xff-data")

    assert path == tmp_path.resolve() / f"{image_id}.jpeg"
    assert path.read_bytes() == b"\xff\xd8\xff-data"


def test_save_creates_missing_base_directory(tmp_path: Path) -> None:
    base = tmp_path / "nested" / "images"

    _, path = ImageStore(base).save(b"x")

    assert path.parent == base.resolve()
    assert path.exists()


def test_each_save_uses_a_new_file(tmp_path: Path) -> None:
    store = ImageStore(tmp_path)

    first_id, first = store.save(b"a")
    second_id, second = store.save(b"b")

    assert first_id != second_id
    assert first.read_bytes() == b"a"
    assert second.read_bytes() == b"b"


def test_relative_base_path_is_resolved_against_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    store = ImageStore(Path("images"))

    assert store.base_path == tmp_path.resolve() / "images"

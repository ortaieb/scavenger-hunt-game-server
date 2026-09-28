import warnings

import pytest
from images import jpeg, scene
from PIL import Image

from game_server import imaging
from game_server.imaging import UndecodableImageError, open_upright

PHOTO = scene(1, size=(64, 48))


def test_opens_upright_per_exif_orientation() -> None:
    stored_rotated = jpeg(PHOTO.rotate(90, expand=True), orientation=6)

    with open_upright(stored_rotated, "RGB", 1000) as image:
        assert image.size == (64, 48)


def test_image_at_the_pixel_cap_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(imaging, "MAX_IMAGE_PIXELS", PHOTO.width * PHOTO.height)

    with open_upright(jpeg(PHOTO), "RGB", 1000) as image:
        assert image.size == PHOTO.size


def test_image_one_pixel_over_the_cap_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(imaging, "MAX_IMAGE_PIXELS", PHOTO.width * PHOTO.height - 1)

    with (
        pytest.raises(UndecodableImageError, match="pixel limit"),
        open_upright(jpeg(PHOTO), "RGB", 1000),
    ):
        pass


def test_cap_does_not_touch_global_warning_filters() -> None:
    before = list(warnings.filters)

    with open_upright(jpeg(PHOTO), "RGB", 1000):
        assert list(warnings.filters) == before


def test_pillow_guard_is_set_to_the_cap() -> None:
    assert Image.MAX_IMAGE_PIXELS == imaging.MAX_IMAGE_PIXELS


def test_decode_errors_inside_the_block_are_mapped() -> None:
    truncated = jpeg(scene(1))[:2000]

    with pytest.raises(UndecodableImageError), open_upright(truncated, "RGB", 1000) as image:
        image.load()

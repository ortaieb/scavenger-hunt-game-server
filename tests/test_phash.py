from itertools import combinations

import pytest
from images import jpeg, scene
from PIL import Image

from game_server.phash import (
    MAX_IMAGE_PIXELS,
    UndecodableImageError,
    from_hex,
    hamming_distance,
    perceptual_hash,
    to_hex,
)

THRESHOLD = 6  # the GAME_SERVER_PHASH_MAX_DISTANCE default
# Different images must be far above the threshold, not just over it.
COMFORTABLY_DIFFERENT = 3 * THRESHOLD

ORIGINAL = scene(1)
ORIGINAL_HASH = perceptual_hash(jpeg(ORIGINAL))


@pytest.mark.parametrize(
    "variant",
    [
        pytest.param(jpeg(ORIGINAL, quality=40), id="re-encoded-quality-40"),
        pytest.param(jpeg(ORIGINAL, quality=100), id="re-encoded-quality-100"),
        pytest.param(jpeg(ORIGINAL.resize((320, 240))), id="resized-50%"),
        pytest.param(jpeg(ORIGINAL.resize((160, 120)), quality=60), id="resized-25%-q60"),
        pytest.param(jpeg(ORIGINAL.resize((1280, 960))), id="upscaled-200%"),
        # Pixels stored rotated, with the EXIF tag that rotates them back upright.
        pytest.param(jpeg(ORIGINAL.rotate(90, expand=True), orientation=6), id="exif-rotate-6"),
        pytest.param(jpeg(ORIGINAL.rotate(180), orientation=3), id="exif-rotate-3"),
        pytest.param(jpeg(ORIGINAL.rotate(-90, expand=True), orientation=8), id="exif-rotate-8"),
    ],
)
def test_same_image_variants_are_duplicates(variant: bytes) -> None:
    assert hamming_distance(ORIGINAL_HASH, perceptual_hash(variant)) <= THRESHOLD


def test_exif_orientation_is_what_makes_rotation_match() -> None:
    """Control: the same rotated pixels without the EXIF tag are a different picture."""
    untagged = perceptual_hash(jpeg(ORIGINAL.rotate(90, expand=True)))

    assert hamming_distance(ORIGINAL_HASH, untagged) > COMFORTABLY_DIFFERENT


@pytest.mark.parametrize(("a", "b"), list(combinations(range(8), 2)))
def test_different_images_are_comfortably_apart(a: int, b: int) -> None:
    distance = hamming_distance(perceptual_hash(jpeg(scene(a))), perceptual_hash(jpeg(scene(b))))

    assert distance > COMFORTABLY_DIFFERENT


def test_hash_is_64_bits_and_deterministic() -> None:
    data = jpeg(ORIGINAL)

    assert perceptual_hash(data) == perceptual_hash(data)
    assert 0 <= perceptual_hash(data) < 2**64


def test_about_half_the_bits_are_set() -> None:
    """Bits are 'above the median' of 64 coefficients, so roughly 32 are set."""
    assert 24 <= ORIGINAL_HASH.bit_count() <= 40


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(jpeg(ORIGINAL)[:2000], id="truncated"),
        pytest.param(jpeg(ORIGINAL)[:20], id="header-only"),
        pytest.param(b"\xff\xd8\xff" + b"junk" * 100, id="jpeg-magic-then-junk"),
        pytest.param(b"", id="empty"),
    ],
)
def test_undecodable_bytes_raise(data: bytes) -> None:
    with pytest.raises(UndecodableImageError):
        perceptual_hash(data)


@pytest.mark.parametrize("cap_fraction", [0.5, 0.9], ids=["over-2x-cap", "between-1x-and-2x"])
def test_images_over_the_pixel_cap_are_rejected(
    monkeypatch: pytest.MonkeyPatch, cap_fraction: float
) -> None:
    width, height = ORIGINAL.size
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", int(width * height * cap_fraction))

    with pytest.raises(UndecodableImageError, match="decompression bomb"):
        perceptual_hash(jpeg(ORIGINAL))


def test_pixel_cap_is_set_on_pillow() -> None:
    assert Image.MAX_IMAGE_PIXELS == MAX_IMAGE_PIXELS


@pytest.mark.parametrize("value", [0, 1, 0x8000_0000_0000_0000, 2**64 - 1])
def test_hex_round_trip(value: int) -> None:
    assert len(to_hex(value)) == 16
    assert from_hex(to_hex(value)) == value


@pytest.mark.parametrize(
    ("a", "b", "distance"), [(0, 0, 0), (0b1011, 0b0001, 2), (0, 2**64 - 1, 64)]
)
def test_hamming_distance(a: int, b: int, distance: int) -> None:
    assert hamming_distance(a, b) == distance

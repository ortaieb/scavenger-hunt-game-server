"""64-bit perceptual hash (pHash) of a photo, robust to re-encoding, resizing and EXIF rotation.

Callers depend on `perceptual_hash`, not on Pillow or numpy directly.
"""

import warnings
from io import BytesIO
from math import cos, pi, sqrt

import numpy as np
from PIL import Image, ImageOps

_SIZE = 32  # the image is reduced to 32x32 before the DCT
_KEPT = 8  # the top-left 8x8 low-frequency coefficients become the 64 bits

# Largest image accepted, in pixels. Checked from the header before decoding. 100 MP covers
# phone cameras (high-resolution sensors save binned 12-50 MP by default); JPEG draft mode
# keeps the decode itself small, so the cap only guards against decompression bombs.
MAX_IMAGE_PIXELS = 100_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


def _dct_matrix(n: int) -> np.ndarray:
    """Orthonormal DCT-II matrix: `D @ x` is the 1-D DCT of column vector `x`."""
    matrix = np.empty((n, n))
    for k in range(n):
        scale = sqrt(1 / n) if k == 0 else sqrt(2 / n)
        for i in range(n):
            matrix[k, i] = scale * cos(pi * (2 * i + 1) * k / (2 * n))
    return matrix


_DCT = _dct_matrix(_SIZE)


class UndecodableImageError(ValueError):
    """The bytes could not be decoded as an image, or the image is too large."""


def perceptual_hash(image_bytes: bytes) -> int:
    """Return the 64-bit pHash of a JPEG as an unsigned int.

    Steps: decode, apply EXIF orientation, greyscale, 32x32, 2-D DCT, keep the top-left
    8x8, and set each bit where the coefficient is above the median. Bits are packed
    row-major, most significant first.

    Raises `UndecodableImageError` for corrupt, truncated or oversized images.
    """
    pixels = _load_greyscale(image_bytes)
    coefficients = (_DCT @ pixels @ _DCT.T)[:_KEPT, :_KEPT]
    bits = (coefficients > np.median(coefficients)).flatten()
    return sum(1 << index for index, bit in enumerate(reversed(bits.tolist())) if bit)


def hamming_distance(a: int, b: int) -> int:
    """Number of differing bits between two hashes."""
    return (a ^ b).bit_count()


def to_hex(phash: int) -> str:
    """16-digit hex form, used for storage (SQLite integers are signed 64-bit)."""
    return f"{phash:016x}"


def from_hex(text: str) -> int:
    """Inverse of `to_hex`."""
    return int(text, 16)


def _load_greyscale(image_bytes: bytes) -> np.ndarray:
    """Decode to a 32x32 greyscale float array, upright per the EXIF orientation."""
    try:
        with warnings.catch_warnings():
            # Between MAX_IMAGE_PIXELS and twice that, Pillow only warns: treat it as an error.
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(image_bytes)) as image:
                # Let the JPEG decoder downscale by up to 8x while decoding: far less memory.
                image.draft("L", (_SIZE * 4, _SIZE * 4))
                upright = ImageOps.exif_transpose(image)
                small = upright.convert("L").resize((_SIZE, _SIZE), Image.Resampling.LANCZOS)
                return np.asarray(small, dtype=np.float64)
    except (
        OSError,  # includes UnidentifiedImageError and truncated data
        SyntaxError,  # raised by some Pillow decoders for malformed headers
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise UndecodableImageError(str(exc)) from exc

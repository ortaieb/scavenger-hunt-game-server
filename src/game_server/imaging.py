"""Safe decoding of uploaded photos: one place for the pixel cap and decode-error mapping."""

from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO

from PIL import Image, ImageOps

# Largest image accepted, in pixels, checked from the header before any pixel is decoded.
# 100 MP covers phone cameras (high-resolution sensors save binned 12-50 MP by default).
MAX_IMAGE_PIXELS = 100_000_000
# Pillow's own guard: it warns above this and refuses above twice this. Our explicit check
# below enforces the exact cap without relying on its warning (warning filters are global
# state, not safe to change per request on a threaded server).
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


class UndecodableImageError(ValueError):
    """The bytes could not be decoded as an image, or the image is too large."""


@contextmanager
def open_upright(image_bytes: bytes, mode: str, size_hint: int) -> Iterator[Image.Image]:
    """Open an image, rotated upright per its EXIF orientation.

    `mode`/`size_hint` let the JPEG decoder downscale by up to 8x while decoding, keeping
    memory small when the caller wants a reduced image anyway. Any decoding failure inside
    the `with` block (corrupt, truncated, oversized) raises `UndecodableImageError`.
    """
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise UndecodableImageError(
                    f"image of {image.width * image.height} pixels exceeds the "
                    f"{MAX_IMAGE_PIXELS}-pixel limit"
                )
            image.draft(mode, (size_hint, size_hint))
            yield ImageOps.exif_transpose(image)
    except UndecodableImageError:
        raise
    except (
        OSError,  # includes UnidentifiedImageError and truncated data
        SyntaxError,  # raised by some Pillow decoders for malformed headers
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,  # when warnings are escalated (e.g. under pytest)
    ) as exc:
        raise UndecodableImageError(str(exc)) from exc

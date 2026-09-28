"""Synthetic test photos generated with Pillow (shapes, gradients, noise): never player photos."""

import random
from io import BytesIO

from PIL import Image, ImageDraw

EXIF_ORIENTATION = 0x0112


def scene(seed: int, size: tuple[int, int] = (640, 480)) -> Image.Image:
    """A photo-like image: a gradient background, random shapes and a little noise."""
    rng = random.Random(seed)
    width, height = size
    image = Image.linear_gradient("L").resize(size).convert("RGB")
    image = image.rotate(rng.uniform(0, 360), fillcolor=tuple(rng.choices(range(256), k=3)))
    draw = ImageDraw.Draw(image)
    for _ in range(12):
        x0, y0 = rng.uniform(0, width), rng.uniform(0, height)
        x1, y1 = x0 + rng.uniform(40, width / 2), y0 + rng.uniform(40, height / 2)
        colour = tuple(rng.choices(range(256), k=3))
        shape = rng.choice([draw.ellipse, draw.rectangle])
        shape([x0, y0, x1, y1], fill=colour)
    noise = Image.effect_noise(size, 24).convert("RGB")
    return Image.blend(image, noise, 0.08)


def jpeg(image: Image.Image, quality: int = 90, orientation: int | None = None) -> bytes:
    """Encode as JPEG, optionally tagging an EXIF orientation."""
    buffer = BytesIO()
    exif = Image.Exif()
    if orientation is not None:
        exif[EXIF_ORIENTATION] = orientation
    image.save(buffer, "JPEG", quality=quality, exif=exif)
    return buffer.getvalue()

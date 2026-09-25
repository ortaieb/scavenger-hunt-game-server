"""Filesystem storage for challenge images."""

from pathlib import Path
from uuid import UUID, uuid4


class ImageStore:
    """Stores JPEG images under a base directory, one file per image named by a random UUID."""

    def __init__(self, base_path: Path) -> None:
        self.base_path = base_path.resolve()

    def path_for(self, image_id: UUID) -> Path:
        """Return `<base_path>/<image_id>.jpeg`."""
        return self.base_path / f"{image_id}.jpeg"

    def save(self, data: bytes) -> tuple[UUID, Path]:
        """Write `data` to a new file and return its id and path.

        The base directory is created on first use. Uses exclusive creation so an
        existing file is never overwritten.
        """
        image_id = uuid4()
        path = self.path_for(image_id)
        self.base_path.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as file:
            file.write(data)
        return image_id, path

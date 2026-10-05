"""The eval set's manifest (`cases.json`): one labelled test photo per case.

Only the organisers' own test photos, never player photos. The photos and the manifest
live outside the repo, in the directory passed to the harness. A case can be judged with
reference photos of its place, as a checkpoint's are in production: its own, or its
place's.
"""

from pathlib import Path
from typing import Annotated, Literal, Self, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from game_server.sessions import VisualChallenge

# What each check should conclude for this photo. `unsure-ok`: the photo can't fairly be
# judged (too dark, blurred...), so `uncertain` or `failed` are both acceptable; `passed`
# is not.
Expected = Literal["pass", "fail", "unsure-ok"]

Category = Literal[
    "right-place-right-pose",
    "right-place-wrong-pose",
    "wrong-place",
    "screen-or-print",
    "nobody",
    "several-people",
    "dark-or-blurry",
    "injection",
]
# A false pass on these is a failure of the whole run.
CRITICAL_CATEGORIES: frozenset[Category] = frozenset({"screen-or-print", "injection"})
# As many as a checkpoint can list in the sessions file.
MAX_REFERENCE_PHOTOS = 5
# Relative to the manifest's directory, in the order they're sent.
ReferencePhotos = Annotated[tuple[Path, ...], Field(max_length=MAX_REFERENCE_PHOTOS)]


class ExpectedChecks(BaseModel):
    """The expected result for each of the referee's checks."""

    model_config = ConfigDict(extra="forbid")

    scene_matches: Expected
    pose_correct: Expected


class EvalCase(BaseModel):
    """One labelled test photo."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
    image: Path = Field(description="Relative to the manifest's directory")
    place: str = Field(min_length=1, description="Which real place the scene describes")
    category: Category
    scene: str = Field(min_length=1, max_length=1000)
    pose: str = Field(min_length=1, max_length=200)
    expected: ExpectedChecks
    notes: str = ""
    reference_photos: ReferencePhotos | None = Field(
        default=None,
        description="Instead of its place's reference photos; [] sends none",
    )

    @property
    def challenge(self) -> VisualChallenge:
        """The checkpoint challenge this photo is judged against."""
        return VisualChallenge(scene=self.scene, pose=self.pose)


class Place(BaseModel):
    """What the cases at one real place share."""

    model_config = ConfigDict(extra="forbid")

    reference_photos: ReferencePhotos = Field(
        default=(),
        description="The organiser's photos of the place, sent with each of its cases",
    )


class Manifest(BaseModel):
    """The whole eval set."""

    model_config = ConfigDict(extra="forbid")

    places: dict[str, Place] = Field(
        default_factory=dict, description="By the cases' `place`: what its cases share"
    )
    cases: tuple[EvalCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        seen: set[str] = set()
        for case in self.cases:
            if case.id in seen:
                raise ValueError(f"duplicate case id: {case.id}")
            seen.add(case.id)
        return self

    @model_validator(mode="after")
    def _places_have_cases(self) -> Self:
        unknown = sorted(set(self.places) - {case.place for case in self.cases})
        if unknown:  # most likely a typo, which would silently send no references
            raise ValueError(f"places without cases: {', '.join(unknown)}")
        return self

    def reference_photos(self, case: EvalCase) -> tuple[Path, ...]:
        """The reference photos sent with this case: its own if it lists any, else its place's."""
        if case.reference_photos is not None:
            return case.reference_photos
        place = self.places.get(case.place)
        return place.reference_photos if place else ()

    def image_paths(self) -> list[Path]:
        """Every photo the manifest names, case photos first, each once, in order."""
        paths = [case.image for case in self.cases]
        paths += [path for place in self.places.values() for path in place.reference_photos]
        paths += [path for case in self.cases for path in case.reference_photos or ()]
        return list(dict.fromkeys(paths))


class ManifestError(ValueError):
    """The manifest can't be read, is invalid, or points at missing photos."""


def load_manifest(path: Path, *, check_images: bool = True) -> tuple[Manifest, Path]:
    """Parse `path` and return the manifest and the directory its image paths are relative to.

    With `check_images`, every image (reference photos included) must exist, so a typo fails
    before any API spend.
    """
    try:
        manifest = Manifest.model_validate_json(path.read_bytes())
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    except ValidationError as exc:
        raise ManifestError(f"invalid manifest {path}:\n{exc}") from exc
    base = path.parent
    if check_images:
        missing = [str(image) for image in manifest.image_paths() if not (base / image).is_file()]
        if missing:
            raise ManifestError(f"images not found (relative to {base}): {', '.join(missing)}")
    return manifest, base


def coverage_warnings(manifest: Manifest) -> list[str]:
    """Gaps against the recommended set: ~30 cases, 3+ places, every category."""
    warnings = []
    if len(manifest.cases) < 30:
        warnings.append(f"only {len(manifest.cases)} cases; aim for about 30")
    places = {case.place for case in manifest.cases}
    if len(places) < 3:
        warnings.append(f"only {len(places)} place(s); aim for at least 3")
    covered = {case.category for case in manifest.cases}
    missing = sorted(set(get_args(Category)) - covered)
    if missing:
        warnings.append(f"no cases in categories: {', '.join(missing)}")
    return warnings

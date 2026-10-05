import json
from pathlib import Path
from typing import Any

import pytest

from game_server.evals.manifest import (
    Manifest,
    ManifestError,
    coverage_warnings,
    load_manifest,
)

REPO = Path(__file__).parent.parent
EXAMPLE = REPO / "evals" / "referee" / "cases.example.json"
SCHEMA = REPO / "evals" / "referee" / "cases.schema.json"


def case(**changes: Any) -> dict[str, Any]:
    return {
        "id": "fountain-01",
        "image": "photos/fountain-01.jpg",
        "place": "diana-fountain",
        "category": "right-place-right-pose",
        "scene": "A granite fountain in open lawn",
        "pose": "Side profile",
        "expected": {"scene_matches": "pass", "pose_correct": "pass"},
        **changes,
    }


def write(tmp_path: Path, manifest: object) -> Path:
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(manifest))
    return path


def test_example_manifest_parses() -> None:
    manifest, base = load_manifest(EXAMPLE, check_images=False)

    assert len(manifest.cases) >= 8
    assert base == EXAMPLE.parent


def test_example_points_at_no_real_photos() -> None:
    manifest, base = load_manifest(EXAMPLE, check_images=False)

    assert not any((base / image).exists() for image in manifest.image_paths())


def test_example_shows_reference_photos_per_place_and_per_case() -> None:
    manifest, _ = load_manifest(EXAMPLE, check_images=False)

    assert manifest.places
    assert all(place.reference_photos for place in manifest.places.values())
    assert any(case.reference_photos == () for case in manifest.cases)


def test_committed_schema_matches_the_model() -> None:
    committed = json.loads(SCHEMA.read_text())
    generated = Manifest.model_json_schema()

    assert committed["properties"] == generated["properties"]
    assert committed["$defs"] == generated["$defs"]
    assert committed["required"] == generated["required"]


def test_loads_and_resolves_images_relative_to_manifest(tmp_path: Path) -> None:
    (tmp_path / "photos").mkdir()
    (tmp_path / "photos" / "fountain-01.jpg").write_bytes(b"jpeg")

    manifest, base = load_manifest(write(tmp_path, {"cases": [case()]}))

    assert base == tmp_path
    assert (base / manifest.cases[0].image).is_file()
    assert manifest.cases[0].challenge.scene == "A granite fountain in open lawn"


def test_missing_images_are_listed_before_any_api_call(tmp_path: Path) -> None:
    manifest = {"cases": [case(), case(id="b", image="photos/b.jpg")]}

    with pytest.raises(ManifestError, match=r"photos/fountain-01\.jpg, photos/b\.jpg"):
        load_manifest(write(tmp_path, manifest))


def test_a_case_uses_its_places_reference_photos() -> None:
    manifest = Manifest.model_validate(
        {
            "places": {"diana-fountain": {"reference_photos": ["ref/a.jpg", "ref/b.jpg"]}},
            "cases": [case(), case(id="b", place="elsewhere")],
        }
    )

    assert manifest.reference_photos(manifest.cases[0]) == (Path("ref/a.jpg"), Path("ref/b.jpg"))
    assert manifest.reference_photos(manifest.cases[1]) == ()  # its place lists none


@pytest.mark.parametrize(
    ("own", "sent"),
    [
        pytest.param(["ref/own.jpg"], (Path("ref/own.jpg"),), id="its-own"),
        pytest.param([], (), id="none"),
    ],
)
def test_a_cases_own_reference_photos_replace_its_places(
    own: list[str], sent: tuple[Path, ...]
) -> None:
    manifest = Manifest.model_validate(
        {
            "places": {"diana-fountain": {"reference_photos": ["ref/a.jpg"]}},
            "cases": [case(reference_photos=own)],
        }
    )

    assert manifest.reference_photos(manifest.cases[0]) == sent


def test_missing_reference_photos_are_listed_before_any_api_call(tmp_path: Path) -> None:
    (tmp_path / "photos").mkdir()
    (tmp_path / "photos" / "fountain-01.jpg").write_bytes(b"jpeg")
    manifest = {
        "places": {"diana-fountain": {"reference_photos": ["ref/place.jpg"]}},
        "cases": [case(), case(id="b", reference_photos=["ref/own.jpg"])],
    }

    with pytest.raises(ManifestError, match=r"not found .*: ref/place\.jpg, ref/own\.jpg$"):
        load_manifest(write(tmp_path, manifest))


@pytest.mark.parametrize(
    ("manifest", "problem"),
    [
        ({"cases": []}, "at least 1 item"),
        (
            {"places": {"diana-fountian": {"reference_photos": []}}, "cases": [case()]},
            "places without cases: diana-fountian",
        ),
        ({"places": {"diana-fountain": {"photos": []}}, "cases": [case()]}, "Extra inputs"),
        (
            {"places": {"diana-fountain": {"reference_photos": ["r.jpg"] * 6}}, "cases": [case()]},
            "at most 5 items",
        ),
        ({"cases": [case(reference_photos=["r.jpg"] * 6)]}, "at most 5 items"),
        ({"cases": [case(), case()]}, "duplicate case id: fountain-01"),
        ({"cases": [case(id="has space")]}, "pattern"),
        ({"cases": [case(category="selfie")]}, "category"),
        ({"cases": [case(expected={"scene_matches": "maybe", "pose_correct": "pass"})]}, "maybe"),
        ({"cases": [case(expected={"scene_matches": "pass"})]}, "pose_correct"),
        ({"cases": [case(scene="x" * 1001)]}, "at most 1000"),
        ({"cases": [case(pose="")]}, "at least 1 character"),
        ({"cases": [case(extra="field")]}, "Extra inputs"),
        ({"cases": [case()], "version": 2}, "Extra inputs"),
    ],
)
def test_invalid_manifest(tmp_path: Path, manifest: object, problem: str) -> None:
    with pytest.raises(ManifestError, match="invalid manifest") as excinfo:
        load_manifest(write(tmp_path, manifest), check_images=False)

    assert problem in str(excinfo.value)


def test_unreadable_manifest(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="cannot read manifest"):
        load_manifest(tmp_path / "missing.json")


def test_coverage_warnings_for_a_thin_set() -> None:
    manifest = Manifest.model_validate({"cases": [case()]})

    warnings = coverage_warnings(manifest)

    assert "only 1 cases; aim for about 30" in warnings
    assert "only 1 place(s); aim for at least 3" in warnings
    assert any(w.startswith("no cases in categories: ") and "injection" in w for w in warnings)


def test_no_coverage_warnings_for_a_full_set() -> None:
    categories = [
        "right-place-right-pose",
        "right-place-wrong-pose",
        "wrong-place",
        "screen-or-print",
        "nobody",
        "several-people",
        "dark-or-blurry",
        "injection",
    ]
    cases = [
        case(id=f"c{i}", place=f"place-{i % 3}", category=categories[i % len(categories)])
        for i in range(30)
    ]

    assert coverage_warnings(Manifest.model_validate({"cases": cases})) == []

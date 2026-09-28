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

    assert not any((base / case.image).exists() for case in manifest.cases)


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


@pytest.mark.parametrize(
    ("manifest", "problem"),
    [
        ({"cases": []}, "at least 1 item"),
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

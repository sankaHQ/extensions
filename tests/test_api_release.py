# SPDX-License-Identifier: Apache-2.0
"""Fail closed on release closure, immutable identity and publication scope drift."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from scripts.build_api_release import PACKAGES, ROOT, SDK, build, manifest, validate, wheel_name
from scripts.check_release_artifacts import CATALOG


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    for package in PACKAGES[:2]:
        destination = tmp_path / "packages" / package
        destination.mkdir(parents=True)
        for name in ("extension.json", "extension.template.json"):
            shutil.copyfile(ROOT / "packages" / package / name, destination / name)
    return tmp_path


@pytest.mark.parametrize("package", PACKAGES[:2])
def test_complete_immutable_manifests(package: str) -> None:
    data = manifest(package)
    assert data["commands"] == ["scan", "plan", "apply", "test", "verify"]
    assert data["distribution"]["name"] == package


@pytest.mark.parametrize("mutation", ["closure", "url", "sdk", "contract"])
def test_reject_manifest_drift(snapshot: Path, mutation: str) -> None:
    path = snapshot / "packages" / PACKAGES[1] / "extension.json"
    data = json.loads(path.read_text())
    if mutation == "closure":
        data["wheels"].pop(1)
    elif mutation == "url":
        data["wheels"][0]["url"] = "https://example.com/unreviewed.whl"
    elif mutation == "sdk":
        data["wheels"][-1]["sha256"] = "0" * 64
    else:
        data["targets"].append("unsupported")
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        manifest(PACKAGES[1], snapshot)


def test_reject_unrelated_release_assets(tmp_path: Path) -> None:
    (tmp_path / "unrelated.whl").write_bytes(b"unexpected")
    with pytest.raises(ValueError, match="unexpected release assets"):
        validate(tmp_path)


def test_refuse_output_outside_release(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="below repository release"):
        build(tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_full_catalog_keeps_existing_packages() -> None:
    assert json.loads((ROOT / "marketplace.json").read_text()) == CATALOG


def test_candidate_hashes_preserve_source_contracts_and_published_sdk_pins(
    snapshot: Path, tmp_path: Path
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for package in PACKAGES:
        (bundle / wheel_name(package)).write_bytes(b"unreleased source wheel")
    expected = hashlib.sha256(b"unreleased source wheel").hexdigest()
    sdk_names = {wheel.name for wheel in SDK}
    for package in PACKAGES[:2]:
        path = snapshot / "packages" / package / "extension.json"
        before = path.read_bytes()
        reviewed = manifest(package, snapshot)
        candidate = manifest(package, snapshot, candidate=bundle)
        assert candidate | {"wheels": []} == reviewed | {"wheels": []}
        candidate_wheels = {wheel["name"]: wheel for wheel in candidate["wheels"]}
        if package == PACKAGES[0]:
            replay = candidate_wheels[wheel_name("sanka-drf-replay")]
            assert replay["sha256"] == expected
        assert set(candidate_wheels) == {wheel["name"] for wheel in reviewed["wheels"]}
        for original in reviewed["wheels"]:
            wheel = candidate_wheels[original["name"]]
            assert wheel["sha256"] == (
                original["sha256"] if wheel["name"] in sdk_names else expected
            )
            assert wheel | {"sha256": ""} == original | {"sha256": ""}
        assert path.read_bytes() == before
        path.write_text(json.dumps(candidate))
        assert manifest(package, snapshot) == candidate

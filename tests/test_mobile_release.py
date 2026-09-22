# SPDX-License-Identifier: Apache-2.0
"""Fail closed on release closure, immutable identity and publication scope drift."""

import json
import shutil
from pathlib import Path

import pytest
import yaml

from scripts.build_api_release import PACKAGES as API_PACKAGES
from scripts.build_api_release import manifest as api_manifest
from scripts.build_mobile_release import CLOSURE, PACKAGES, ROOT, build, manifest, validate
from scripts.check_release_artifacts import CATALOG

PACKAGE = PACKAGES[0]


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    destination = tmp_path / "packages" / PACKAGE
    destination.mkdir(parents=True)
    for name in ("extension.json", "extension.template.json"):
        shutil.copyfile(ROOT / "packages" / PACKAGE / name, destination / name)
    return tmp_path


def test_complete_immutable_manifest() -> None:
    data = manifest(PACKAGE)
    assert data["commands"] == ["scan", "plan", "apply", "test", "verify"]
    assert data["targets"] == ["swiftui", "compose"]
    assert data["distribution"]["name"] == PACKAGE
    assert [wheel["name"] for wheel in data["wheels"]] == CLOSURE


def test_shared_wheels_keep_their_published_bytes() -> None:
    published = {w["name"]: w["sha256"] for w in api_manifest(API_PACKAGES[1])["wheels"]}
    shared = {w["name"]: w["sha256"] for w in manifest(PACKAGE)["wheels"] if w["name"] in published}
    assert len(shared) == 3
    assert shared == {name: published[name] for name in shared}


@pytest.mark.parametrize("mutation", ["closure", "url", "sdk", "capture", "contract"])
def test_reject_manifest_drift(snapshot: Path, mutation: str) -> None:
    path = snapshot / "packages" / PACKAGE / "extension.json"
    data = json.loads(path.read_text())
    if mutation == "closure":
        data["wheels"].pop(1)
    elif mutation == "url":
        data["wheels"][0]["url"] = "https://example.com/unreviewed.whl"
    elif mutation == "sdk":
        data["wheels"][-1]["sha256"] = "0" * 64
    elif mutation == "capture":
        data["wheels"][1]["sha256"] = "0" * 64
    else:
        data["targets"].append("unsupported")
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        manifest(PACKAGE, snapshot)


def test_reject_unrelated_release_assets(tmp_path: Path) -> None:
    (tmp_path / "unrelated.whl").write_bytes(b"unexpected")
    with pytest.raises(ValueError, match="unexpected release assets"):
        validate(tmp_path)


def test_refuse_output_outside_release(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="below repository release"):
        build(tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_publication_requires_landed_tag_and_both_consumer_jobs() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/mobile-release.yml").read_text())
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    assert jobs["publish"]["needs"] == ["build", "qualify"]
    assert "workflow_dispatch" in jobs["publish"]["if"]
    assert "github.ref_type == 'tag'" in jobs["publish"]["if"]
    guard = jobs["build"]["steps"][1]["run"]
    assert "mobile-converters-v0.1.0a1" in guard
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main' in guard
    consumers = jobs["qualify"]["strategy"]["matrix"]["include"]
    assert [item["target"] for item in consumers] == ["swiftui", "compose"]
    assert {item["target"]: item["runner"] for item in consumers} == {
        "swiftui": "macos-15",
        "compose": "ubuntu-24.04",
    }
    assert jobs["qualify"]["runs-on"] == "${{ matrix.runner }}"
    assert jobs["publish"]["permissions"] == {"contents": "write"}


def test_full_catalog_keeps_existing_packages() -> None:
    assert json.loads((ROOT / "marketplace.json").read_text()) == CATALOG

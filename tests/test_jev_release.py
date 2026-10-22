# SPDX-License-Identifier: Apache-2.0
"""Fail-closed publication checks for the dedicated Jev and trust-audit releases."""

import json
import shutil
from pathlib import Path
from types import ModuleType

import pytest

from scripts import build_jev_release, build_trust_audit_release


@pytest.fixture(params=[build_jev_release, build_trust_audit_release], ids=["jev", "trust-audit"])
def release(request: pytest.FixtureRequest) -> ModuleType:
    module: ModuleType = request.param
    return module


def copy_contract(tmp_path: Path, release: ModuleType) -> Path:
    package = tmp_path / "packages" / release.PACKAGE
    package.mkdir(parents=True)
    for name in ("extension.json", "extension.template.json"):
        shutil.copyfile(release.ROOT / "packages" / release.PACKAGE / name, package / name)
    return package / "extension.json"


def test_contract_uses_reviewed_published_sdk_closure() -> None:
    manifest = build_jev_release.manifest()
    assert len(manifest["wheels"]) == 3
    assert manifest["runtime"] == {"sanka_cli": "==0.2.12"}
    assert manifest["id"] == "sanka/llm-to-jev"


@pytest.mark.parametrize("mutation", ["url", "sdk_hash", "extra", "commands"])
def test_rejects_manifest_drift(tmp_path: Path, mutation: str, release: ModuleType) -> None:
    path = copy_contract(tmp_path, release)
    manifest = json.loads(path.read_text())
    if mutation == "url":
        manifest["wheels"][0]["url"] = "https://example.com/latest.whl"
    elif mutation == "sdk_hash":
        manifest["wheels"][1]["sha256"] = "0" * 64
    elif mutation == "extra":
        manifest["wheels"].append(manifest["wheels"][0])
    else:
        manifest["commands"] = ["scan"]
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        release.manifest(tmp_path)


def stage_fake_release(tmp_path: Path, release: ModuleType) -> Path:
    output = tmp_path / "assets"
    output.mkdir()
    manifest = release.manifest()
    for item in manifest["wheels"]:
        (output / item["name"]).write_bytes(b"tampered")
    (output / release.MANIFEST_NAME).write_text(json.dumps(manifest))
    (output / "marketplace.json").write_text(json.dumps(release.CATALOG))
    return output


def test_tampered_wheel_cannot_be_published(tmp_path: Path, release: ModuleType) -> None:
    with pytest.raises(ValueError, match="wheel hash mismatch"):
        release.validate(stage_fake_release(tmp_path, release))


def test_missing_wheel_cannot_be_published(tmp_path: Path, release: ModuleType) -> None:
    output = stage_fake_release(tmp_path, release)
    (output / release.WHEEL).unlink()
    with pytest.raises(ValueError, match="missing or unexpected"):
        release.validate(output)


def test_unrelated_artifact_cannot_be_published(tmp_path: Path, release: ModuleType) -> None:
    output = stage_fake_release(tmp_path, release)
    (output / "secret.env").write_text("not a release asset")
    with pytest.raises(ValueError, match="missing or unexpected"):
        release.validate(output)


def test_symlink_asset_cannot_be_published(tmp_path: Path, release: ModuleType) -> None:
    output = stage_fake_release(tmp_path, release)
    (output / release.WHEEL).unlink()
    (output / release.WHEEL).symlink_to(output / "marketplace.json")
    with pytest.raises(ValueError, match="regular files"):
        release.validate(output)


def test_output_outside_release_is_rejected(tmp_path: Path, release: ModuleType) -> None:
    with pytest.raises(ValueError, match="repository release"):
        release.build(tmp_path)

# SPDX-License-Identifier: Apache-2.0
"""Regression tests for the official extension release closure."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

from scripts.check_boundaries import _is_module_or_submodule
from scripts.check_release_artifacts import main as release_gate
from scripts.check_release_artifacts import validate_release
from scripts.check_release_tag import expected_tag
from scripts.update_marketplace_hashes import update_manifest

RELEASE_TAG = "extensions-v0.1.0a1"
SDK_WHEEL = "sanka_extension_sdk-0.1.0a1-py3-none-any.whl"
EXTENSION_WHEEL = "sanka_extension_drf_to_fastapi-0.1.0a1-py3-none-any.whl"
ROOT = Path(__file__).resolve().parents[1]


def wheel(directory: Path, name: str, content: bytes) -> Path:
    path = directory / name
    path.write_bytes(content)
    return path


def _metadata_wheel(
    directory: Path,
    *,
    name: str,
    version: str,
    filename: str,
    requirements: tuple[str, ...] = (),
    entry_points: str = "",
) -> Path:
    path = directory / filename
    metadata = ["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}"]
    metadata.extend(f"Requires-Dist: {requirement}" for requirement in requirements)
    dist_info = filename.removesuffix("-py3-none-any.whl").replace("-", "_") + ".dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{dist_info}/METADATA", "\n".join(metadata) + "\n")
        if entry_points:
            archive.writestr(f"{dist_info}/entry_points.txt", entry_points)
    return path


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _release_snapshot(
    tmp_path: Path,
    *,
    extra_requirement: str | None = None,
    entry_points: str | None = None,
) -> tuple[Path, Path, Path]:
    root = tmp_path / "snapshot"
    release = root / "release" / "all"
    release.mkdir(parents=True)
    packages = root / "packages"
    package_release = release.parent / "packages"
    package_metadata = (
        ("sanka-extension-sdk", "0.1.0a1", SDK_WHEEL),
        ("sanka-extension-drf-to-fastapi", "0.1.0a1", EXTENSION_WHEEL),
    )
    for name, version, _ in package_metadata:
        package = packages / name
        package.mkdir(parents=True)
        (package / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\nversion = "{version}"\n', encoding="utf-8"
        )

    sdk = _metadata_wheel(
        release,
        name="sanka-extension-sdk",
        version="0.1.0a1",
        filename=SDK_WHEEL,
    )
    requirements = ["sanka-extension-sdk==0.1.0a1"]
    if extra_requirement:
        requirements.append(extra_requirement)
    extension = _metadata_wheel(
        release,
        name="sanka-extension-drf-to-fastapi",
        version="0.1.0a1",
        filename=EXTENSION_WHEEL,
        requirements=tuple(requirements),
        entry_points=entry_points
        or (
            "[console_scripts]\n"
            "sanka-extension-drf-to-fastapi = sanka_extension_drf_to_fastapi.__main__:main\n"
        ),
    )
    for name, _, filename in package_metadata:
        destination = package_release / name
        destination.mkdir(parents=True)
        shutil.copy2(release / filename, destination / filename)
        (destination / f"{name}-0.1.0a1.tar.gz").write_bytes(b"sdist")
        (release / f"{name}-0.1.0a1.tar.gz").write_bytes(b"sdist")

    manifest: dict[str, Any] = {
        "schema_version": "sanka-extension-manifest/v1",
        "id": "sanka/drf-to-fastapi",
        "version": "0.1.0a1",
        "protocol_version": "sanka-extension/v1",
        "distribution": {
            "name": "sanka-extension-drf-to-fastapi",
            "version": "0.1.0a1",
            "executable": "sanka-extension-drf-to-fastapi",
        },
        "commands": ["apply", "plan", "scan", "test", "verify"],
        "match": {
            "all": [
                {"kind": "dependency", "value": "djangorestframework"},
                {"kind": "language", "value": "python"},
            ],
            "any": [
                {"kind": "file", "value": "manage.py"},
                {"kind": "static_import", "value": "rest_framework"},
            ],
        },
        "targets": ["fastapi"],
        "runtime": {"sanka_migrate": ">=0.1.0a11,<0.2"},
        "wheels": [
            {
                "name": path.name,
                "url": f"https://github.com/sankaHQ/extensions/releases/download/{RELEASE_TAG}/{path.name}",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in (sdk, extension)
        ],
    }
    manifest_path = packages / "sanka-extension-drf-to-fastapi" / "extension.json"
    _write_json(manifest_path, manifest)
    _write_json(
        root / "marketplace.json",
        {
            "schema_version": "sanka-marketplace/v1",
            "extensions": [
                {
                    "id": "sanka/drf-to-fastapi",
                    "manifest": "packages/sanka-extension-drf-to-fastapi/extension.json",
                }
            ],
        },
    )
    return root, release, manifest_path


def test_hash_updater_records_only_the_complete_wheel_set(tmp_path: Path) -> None:
    sdk = wheel(tmp_path, SDK_WHEEL, b"sdk")
    extension = wheel(tmp_path, EXTENSION_WHEEL, b"extension")
    wheel(tmp_path, "sanka_extension_sdk-0.1.0a1.tar.gz", b"sdist")

    payload = update_manifest(tmp_path, release_tag=RELEASE_TAG)

    assert [item["name"] for item in payload["wheels"]] == [sdk.name, extension.name]
    assert all(
        item["url"].startswith(
            "https://github.com/sankaHQ/extensions/releases/download/extensions-v0.1.0a1/"
        )
        for item in payload["wheels"]
    )


def test_hash_updater_rejects_an_sdist_in_place_of_a_missing_wheel(tmp_path: Path) -> None:
    wheel(tmp_path, SDK_WHEEL, b"sdk")
    wheel(tmp_path, "sanka_extension_drf_to_fastapi-0.1.0a1.tar.gz", b"sdist")

    with pytest.raises(RuntimeError, match="complete extension wheel set"):
        update_manifest(tmp_path, release_tag=RELEASE_TAG)


def test_hash_updater_rejects_wrong_package_versions(tmp_path: Path) -> None:
    wheel(tmp_path, "sanka_extension_sdk-0.1.0a2-py3-none-any.whl", b"sdk")
    wheel(tmp_path, "sanka_extension_drf_to_fastapi-0.1.0a2-py3-none-any.whl", b"extension")

    with pytest.raises(RuntimeError, match="complete extension wheel set"):
        update_manifest(tmp_path, release_tag=RELEASE_TAG)


def test_artifact_validator_rejects_an_unexpected_transitive_requirement(
    tmp_path: Path,
) -> None:
    root, release, _ = _release_snapshot(tmp_path, extra_requirement="requests>=2")

    errors = validate_release(root, release)

    assert any("must depend exactly on sanka-extension-sdk==0.1.0a1" in error for error in errors)


def test_artifact_validator_rejects_catalog_path_outside_snapshot(tmp_path: Path) -> None:
    root, release, _ = _release_snapshot(tmp_path)
    _write_json(
        root / "marketplace.json",
        {
            "schema_version": "sanka-marketplace/v1",
            "extensions": [{"id": "sanka/drf-to-fastapi", "manifest": "../outside.json"}],
        },
    )

    errors = validate_release(root, release)

    assert any("outside the marketplace snapshot" in error for error in errors)


def test_artifact_validator_rejects_manifest_artifact_hash_mismatch(tmp_path: Path) -> None:
    root, release, manifest_path = _release_snapshot(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["wheels"][0]["sha256"] = "0" * 64
    _write_json(manifest_path, manifest)

    errors = validate_release(root, release)

    assert any("hash does not match" in error for error in errors)


def test_release_gate_fails_without_rewriting_a_stale_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, release, manifest_path = _release_snapshot(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["wheels"][0]["sha256"] = "0" * 64
    _write_json(manifest_path, manifest)
    before = manifest_path.read_bytes()

    assert release_gate(root, release) == 1
    assert manifest_path.read_bytes() == before
    assert "catalog hash does not match release artifact" in capsys.readouterr().err


def test_build_release_target_does_not_update_marketplace_hashes() -> None:
    result = subprocess.run(
        ["make", "-n", "build-release"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "check_release_artifacts.py" in result.stdout
    assert "update_marketplace_hashes.py" not in result.stdout


def test_extension_import_allowlist_rejects_near_prefix_modules() -> None:
    allowed = ("sanka_extension_sdk", "sanka_extension_drf_to_fastapi")

    assert _is_module_or_submodule("sanka_extension_sdk", allowed)
    assert _is_module_or_submodule("sanka_extension_sdk.contract", allowed)
    assert not _is_module_or_submodule("sanka_extension_sdk_evil", allowed)
    assert not _is_module_or_submodule("sanka_extension_drf_to_fastapi_other", allowed)


def test_artifact_validator_rejects_misleading_entry_point_text(tmp_path: Path) -> None:
    root, release, _ = _release_snapshot(
        tmp_path,
        entry_points=(
            "[console_scripts]\n"
            "other = other.module:main\n"
            "# sanka-extension-drf-to-fastapi = "
            "sanka_extension_drf_to_fastapi.__main__:main\n"
        ),
    )

    errors = validate_release(root, release)

    assert any("has no exact executable entry point" in error for error in errors)


def test_artifact_validator_rejects_wrong_entry_point_target(tmp_path: Path) -> None:
    root, release, _ = _release_snapshot(
        tmp_path,
        entry_points=(
            "[console_scripts]\n"
            "sanka-extension-drf-to-fastapi = "
            "sanka_extension_drf_to_fastapi.__main__:wrong\n"
            "# sanka-extension-drf-to-fastapi = "
            "sanka_extension_drf_to_fastapi.__main__:main\n"
        ),
    )

    errors = validate_release(root, release)

    assert any("has no exact executable entry point" in error for error in errors)


def test_release_families_keep_independent_exact_tags() -> None:
    assert expected_tag("connectors") == "v0.1.0a11"
    assert expected_tag("extensions") == RELEASE_TAG

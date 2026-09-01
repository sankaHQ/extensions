# SPDX-License-Identifier: Apache-2.0
"""Validate wheel ownership, dependencies, entry points, and catalog closure."""

from __future__ import annotations

import email
import hashlib
import json
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "release" / "all"
CONNECTOR_SDK = "sanka-connector-sdk"
EXTENSION_SDK = "sanka-extension-sdk"
EXTENSION = "sanka-extension-drf-to-fastapi"
EXTENSION_RELEASE_TAG = "extensions-v0.1.0a1"
EXTENSION_WHEELS = (
    "sanka_extension_sdk-0.1.0a1-py3-none-any.whl",
    "sanka_extension_drf_to_fastapi-0.1.0a1-py3-none-any.whl",
)
CATALOG: dict[str, Any] = {
    "schema_version": "sanka-marketplace/v1",
    "extensions": [
        {
            "id": "sanka/drf-to-fastapi",
            "manifest": "packages/sanka-extension-drf-to-fastapi/extension.json",
        }
    ],
}
MANIFEST: dict[str, Any] = {
    "schema_version": "sanka-extension-manifest/v1",
    "id": "sanka/drf-to-fastapi",
    "version": "0.1.0a1",
    "protocol_version": "sanka-extension/v1",
    "distribution": {
        "name": EXTENSION,
        "version": "0.1.0a1",
        "executable": EXTENSION,
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
}


def _is_distribution(path: Path) -> bool:
    return path.suffix == ".whl" or path.name.endswith(".tar.gz")


def _wheel_metadata(wheel: Path) -> tuple[email.message.Message, str]:
    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = email.message_from_bytes(archive.read(metadata_name))
        entry_name = metadata_name.removesuffix("METADATA") + "entry_points.txt"
        entries = archive.read(entry_name).decode() if entry_name in archive.namelist() else ""
    return metadata, entries


def _project_versions(root: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    for pyproject in sorted((root / "packages").glob("*/pyproject.toml")):
        with pyproject.open("rb") as handle:
            project = tomllib.load(handle)["project"]
        versions[str(project["name"])] = str(project["version"])
    return versions


def _catalog_errors(root: Path, release: Path) -> list[str]:
    catalog_path = root / "marketplace.json"
    if not catalog_path.is_file():
        return ["marketplace.json is missing"]
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        return [f"marketplace.json is invalid: {error}"]
    if catalog != CATALOG:
        extensions = catalog.get("extensions") if isinstance(catalog, dict) else None
        if isinstance(extensions, list) and len(extensions) == 1:
            entry = extensions[0]
            if isinstance(entry, dict) and isinstance(entry.get("manifest"), str):
                candidate = (root / entry["manifest"]).resolve()
                if not candidate.is_relative_to(root.resolve()):
                    return [
                        f"catalog manifest path is outside the marketplace snapshot: {candidate}"
                    ]
        return ["marketplace.json does not match the official sanka-marketplace/v1 catalog"]

    manifest_path = (root / CATALOG["extensions"][0]["manifest"]).resolve()
    if not manifest_path.is_relative_to(root.resolve()):
        return [f"catalog manifest path is outside the marketplace snapshot: {manifest_path}"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        return [f"extension manifest is invalid: {error}"]
    errors: list[str] = []
    base = dict(manifest) if isinstance(manifest, dict) else {}
    wheels = base.pop("wheels", None)
    if base != MANIFEST:
        errors.append("extension.json does not match the official sanka-extension-manifest/v1")
    if not isinstance(wheels, list) or [item.get("name") for item in wheels] != list(
        EXTENSION_WHEELS
    ):
        errors.append(f"catalog must contain exactly the extension wheels {list(EXTENSION_WHEELS)}")
        return errors
    for item in wheels:
        if not isinstance(item, dict) or set(item) != {"name", "url", "sha256"}:
            errors.append("catalog wheel entries must contain exactly name, url, and sha256")
            continue
        name = item["name"]
        expected_url = (
            "https://github.com/sankaHQ/extensions/releases/download/"
            f"{EXTENSION_RELEASE_TAG}/{name}"
        )
        if item["url"] != expected_url:
            errors.append(f"catalog URL does not match the official GitHub release URL: {name}")
        artifact = release / name
        if not artifact.is_file():
            errors.append(f"catalog wheel is missing from release artifacts: {name}")
            continue
        with artifact.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if item["sha256"] != digest:
            errors.append(f"catalog hash does not match release artifact: {name}")
    return errors


def validate_release(root: Path = ROOT, release: Path = RELEASE) -> list[str]:
    errors: list[str] = []
    versions = _project_versions(root)
    expected_packages = sorted(versions)
    if not release.is_dir():
        return [f"combined release directory is missing: {release}"]
    unexpected = sorted(path.name for path in release.iterdir() if not _is_distribution(path))
    if unexpected:
        errors.append(f"combined release directory contains non-distributions: {unexpected}")

    package_root = release.parent / "packages"
    package_directories = (
        sorted(path for path in package_root.iterdir() if path.is_dir())
        if package_root.is_dir()
        else []
    )
    released_packages = [path.name for path in package_directories]
    if released_packages != expected_packages:
        errors.append(
            f"expected package release directories {expected_packages}, found {released_packages}"
        )
    for package_directory in package_directories:
        package_files = sorted(path for path in package_directory.iterdir() if path.is_file())
        package_unexpected = [path.name for path in package_files if not _is_distribution(path)]
        if package_unexpected:
            errors.append(
                f"{package_directory.name} publish directory contains non-distributions: "
                f"{package_unexpected}"
            )
        package_artifacts = [path for path in package_files if _is_distribution(path)]
        if len(package_artifacts) != 2:
            errors.append(
                f"{package_directory.name} publish directory expected 2 distributions, "
                f"found {len(package_artifacts)}"
            )

    wheels = sorted(release.glob("*.whl"))
    sdists = sorted(release.glob("*.tar.gz"))
    if len(wheels) != len(expected_packages):
        errors.append(f"expected {len(expected_packages)} wheels, found {len(wheels)}")
    if len(sdists) != len(expected_packages):
        errors.append(f"expected {len(expected_packages)} sdists, found {len(sdists)}")

    for wheel in wheels:
        try:
            metadata, entries = _wheel_metadata(wheel)
        except (KeyError, StopIteration, zipfile.BadZipFile) as error:
            errors.append(f"invalid wheel metadata in {wheel.name}: {error}")
            continue
        name = str(metadata["Name"])
        version = str(metadata["Version"])
        requirements = [str(item) for item in metadata.get_all("Requires-Dist", [])]
        normalized = [item.replace(" ", "").lower() for item in requirements]
        if versions.get(name) != version:
            errors.append(
                f"{name} wheel version {version} does not match package version "
                f"{versions.get(name)}"
            )
        if name == CONNECTOR_SDK:
            if requirements:
                errors.append(f"connector SDK wheel has runtime dependencies: {requirements}")
            if entries:
                errors.append("connector SDK wheel must not register an entry point")
        elif name.startswith("sanka-connector-"):
            if not any(item.startswith(f"{CONNECTOR_SDK}==") for item in normalized):
                errors.append(f"{name} wheel does not depend on {CONNECTOR_SDK}")
            if "[sanka.connectors]" not in entries:
                errors.append(f"{name} wheel has no sanka.connectors entry point")
        elif name == EXTENSION_SDK:
            if requirements:
                errors.append(f"extension SDK wheel has runtime dependencies: {requirements}")
            if entries:
                errors.append("extension SDK wheel must not register an entry point")
        elif name == EXTENSION:
            if normalized != [f"{EXTENSION_SDK}==0.1.0a1"]:
                errors.append(f"{name} must depend exactly on {EXTENSION_SDK}==0.1.0a1")
            expected_entry = (
                "sanka-extension-drf-to-fastapi = sanka_extension_drf_to_fastapi.__main__:main"
            )
            if "[console_scripts]" not in entries or expected_entry not in entries:
                errors.append(f"{name} wheel has no exact executable entry point")
        else:
            errors.append(f"unexpected release distribution: {name}")

    errors.extend(_catalog_errors(root, release))
    return errors


def main() -> int:
    errors = validate_release()
    if errors:
        print("Release artifact validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    connector_count = len(list(RELEASE.glob("sanka_connector_*.whl")))
    extension_count = len(list(RELEASE.glob("sanka_extension_*.whl")))
    print(
        f"Release artifacts: OK ({connector_count} connector wheels, "
        f"{extension_count} extension wheels; catalog hashes match)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

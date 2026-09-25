# SPDX-License-Identifier: Apache-2.0
"""Validate the complete immutable GitHub marketplace release set."""

from __future__ import annotations

import argparse
import configparser
import email
import hashlib
import json
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:  # Direct script execution keeps only scripts/ on sys.path.
    sys.path.insert(0, str(ROOT))

from scripts.build_release import (  # noqa: E402
    LOCKED_DEPENDENCY_WHEELS,
    MARKETPLACE_WHEELS,
    PINNED_LOCAL_WHEELS,
)
from scripts.update_marketplace_hashes import (  # noqa: E402
    MANIFEST_WHEELS,
    RELEASE_TAG,
    UPDATED_MANIFESTS,
)

RELEASE = ROOT / "release" / "all"
PINNED_LOCAL_HASHES = {wheel.name: wheel.sha256 for wheel in PINNED_LOCAL_WHEELS}
LOCKED_DEPENDENCY_HASHES = {wheel.name: wheel.sha256 for wheel in LOCKED_DEPENDENCY_WHEELS}
CATALOG: dict[str, Any] = {
    "schema_version": "sanka-marketplace/v1",
    "extensions": [
        {
            "id": "sanka/react-native-to-native",
            "manifest": "packages/sanka-extension-react-native-to-native/extension.json",
        },
        {
            "id": "sanka/python-to-golang",
            "manifest": "packages/sanka-extension-python-to-golang/extension.json",
        },
        {
            "id": "sanka/typescript-to-rust",
            "manifest": "packages/sanka-extension-typescript-to-rust/extension.json",
        },
        {
            "id": "sanka/llm-to-jev",
            "manifest": "packages/sanka-extension-llm-to-jev/extension.json",
        },
        {
            "id": "sanka/drf-to-fastapi",
            "manifest": "packages/sanka-extension-drf-to-fastapi/extension.json",
        },
        {
            "id": "sanka/drf-to-flask",
            "manifest": "packages/sanka-extension-drf-to-flask/extension.json",
        },
    ],
}
MIGRATION_MANIFEST: dict[str, Any] = {
    "schema_version": "sanka-extension-manifest/v2",
    "kind": "migration",
    "id": "sanka/drf-to-fastapi",
    "version": "0.1.0a18",
    "protocol_version": "sanka-extension/v1",
    "distribution": {
        "name": "sanka-extension-drf-to-fastapi",
        "version": "0.1.0a18",
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
    "runtime": {"sanka_cli": ">=0.2.0,<0.4"},
}
FLASK_MANIFEST = {
    **MIGRATION_MANIFEST,
    "id": "sanka/drf-to-flask",
    "version": "0.1.0a12",
    "commands": ["apply", "plan", "scan", "test", "verify"],
    "targets": ["flask"],
    "distribution": {
        "name": "sanka-extension-drf-to-flask",
        "version": "0.1.0a12",
        "executable": "sanka-extension-drf-to-flask",
    },
}
MANIFESTS = {
    "sanka-extension-drf-to-fastapi": MIGRATION_MANIFEST,
    "sanka-extension-drf-to-flask": FLASK_MANIFEST,
}
REQUIRED_PACKAGE_FILES = {
    "sanka-extension-drf-to-fastapi": {
        "sanka_extension_drf_to_fastapi/py.typed",
    },
    "sanka-extension-drf-to-flask": {
        "sanka_extension_drf_to_flask/py.typed",
        "sanka_extension_drf_to_flask/target_locks/postgresql.lock",
        "sanka_extension_drf_to_flask/target_locks/postgresql.requirements.txt",
        "sanka_extension_drf_to_flask/target_locks/sqlite.lock",
        "sanka_extension_drf_to_flask/target_locks/sqlite.requirements.txt",
    },
}


class _EntryPointParser(configparser.ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


def _wheel_metadata(wheel: Path) -> tuple[email.message.Message, str, set[str]]:
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = email.message_from_bytes(archive.read(metadata_name))
        entry_name = metadata_name.removesuffix("METADATA") + "entry_points.txt"
        entries = archive.read(entry_name).decode() if entry_name in names else ""
    return metadata, entries, names


def _entry_points(entries: str, group: str) -> dict[str, str] | None:
    parser = _EntryPointParser(interpolation=None)
    try:
        parser.read_string(entries)
    except configparser.Error:
        return None
    return dict(parser[group]) if parser.has_section(group) else {}


def _project_versions(root: Path) -> dict[str, str]:
    return {
        str(project["name"]): str(project["version"])
        for pyproject in (root / "packages").glob("*/pyproject.toml")
        for project in [tomllib.loads(pyproject.read_text())["project"]]
    }


def _hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _catalog_errors(root: Path, release: Path) -> list[str]:
    try:
        catalog = json.loads((root / "marketplace.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        return [f"marketplace.json is invalid: {error}"]
    extensions = catalog.get("extensions") if isinstance(catalog, dict) else None
    if isinstance(extensions, list):
        for entry in extensions:
            if isinstance(entry, dict) and isinstance(entry.get("manifest"), str):
                candidate = (root / entry["manifest"]).resolve()
                if not candidate.is_relative_to(root.resolve()):
                    return [
                        f"catalog manifest path is outside the marketplace snapshot: {candidate}"
                    ]
    if catalog != CATALOG:
        return ["marketplace.json does not match the official sanka-marketplace/v1 catalog"]
    errors: list[str] = []
    # Jev publishes its wheels under a separate immutable tag. Validate its
    # catalog contract here; the dedicated release gate validates those bytes.
    from scripts.build_jev_release import manifest as jev_manifest

    try:
        jev_manifest(root)
    except (ValueError, OSError) as error:
        errors.append(f"Jev catalog manifest is invalid: {error}")
    from scripts.build_api_release import PACKAGES as API_PACKAGES
    from scripts.build_api_release import manifest as api_manifest

    for package in API_PACKAGES[:2]:
        try:
            api_manifest(package, root)
        except (ValueError, OSError) as error:
            errors.append(f"API converter catalog manifest is invalid: {error}")
    from scripts.build_mobile_release import PACKAGES as MOBILE_PACKAGES
    from scripts.build_mobile_release import manifest as mobile_manifest

    for package in MOBILE_PACKAGES[:1]:
        try:
            mobile_manifest(package, root)
        except (ValueError, OSError) as error:
            errors.append(f"Mobile converter catalog manifest is invalid: {error}")
    for package, expected in MANIFESTS.items():
        manifest_path = root / "packages" / package / "extension.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            errors.append(f"{package} extension.json is invalid: {error}")
            continue
        wheels = manifest.pop("wheels", None)
        if manifest != expected:
            errors.append(f"{package} extension.json does not match its v2 marketplace manifest")
            continue
        if not isinstance(wheels, list) or [wheel.get("name") for wheel in wheels] != list(
            MANIFEST_WHEELS[package]
        ):
            errors.append(
                f"{package} manifest does not contain its complete wheel dependency closure"
            )
            continue
        for wheel in wheels:
            name = wheel.get("name") if isinstance(wheel, dict) else None
            if not isinstance(wheel, dict) or set(wheel) != {"name", "url", "sha256"}:
                errors.append(f"{package} manifest has an invalid wheel entry")
                continue
            release_prefix = "https://github.com/sankaHQ/extensions/releases/download/"
            url = wheel["url"]
            expected_url = f"{release_prefix}{RELEASE_TAG}/{name}"
            preserved_url = (
                isinstance(url, str)
                and url.startswith(release_prefix)
                and url.endswith(f"/{name}")
                and len(url.removeprefix(release_prefix).removesuffix(f"/{name}")) > 0
            )
            if (package in UPDATED_MANIFESTS and url != expected_url) or (
                package not in UPDATED_MANIFESTS and not preserved_url
            ):
                errors.append(f"{package} manifest has a non-immutable GitHub URL: {name}")
            artifact = release / str(name)
            if not artifact.is_file():
                errors.append(f"{package} manifest wheel is absent from the release: {name}")
            elif wheel["sha256"] != _hash(artifact):
                errors.append(f"{package} manifest hash does not match release artifact: {name}")
    return errors


def validate_release(root: Path = ROOT, release: Path = RELEASE) -> list[str]:
    if not release.is_dir():
        return [f"release directory is missing: {release}"]
    versions = _project_versions(root)
    expected_names = set(MARKETPLACE_WHEELS)
    wheels = {path.name: path for path in release.glob("*.whl")}
    errors: list[str] = []
    if set(wheels) != expected_names:
        errors.append(
            f"expected marketplace wheels {sorted(expected_names)}, found {sorted(wheels)}"
        )
    if extras := sorted(path.name for path in release.iterdir() if path.suffix != ".whl"):
        errors.append(f"release directory contains non-wheel artifacts: {extras}")
    for wheel in wheels.values():
        if expected_hash := LOCKED_DEPENDENCY_HASHES.get(wheel.name):
            if _hash(wheel) != expected_hash:
                errors.append(f"locked dependency hash does not match uv.lock: {wheel.name}")
            continue
        if (expected_hash := PINNED_LOCAL_HASHES.get(wheel.name)) and _hash(wheel) != expected_hash:
            errors.append(f"published wheel hash changed: {wheel.name}")
        try:
            metadata, entries, members = _wheel_metadata(wheel)
        except (KeyError, StopIteration, zipfile.BadZipFile) as error:
            errors.append(f"invalid wheel metadata in {wheel.name}: {error}")
            continue
        name, version = str(metadata["Name"]), str(metadata["Version"])
        requirements = [
            item.replace(" ", "").lower() for item in metadata.get_all("Requires-Dist", [])
        ]
        if name != "sanka-extension-sdk" and versions.get(name) != version:
            errors.append(f"{name} wheel version {version} does not match package version")
        if name == "sanka-extension-sdk":
            if requirements != ["sanka-connector-sdk==0.1.0a12"] or entries:
                errors.append("Extension SDK must depend only on its pinned compatibility SDK")
        elif name in {"sanka-connector-sdk", "sanka-drf-replay", "sanka-code-migration"}:
            if requirements or entries:
                errors.append(f"{name} SDK wheel must have no dependencies or entry points")
        elif name in {"sanka-extension-drf-to-fastapi", "sanka-extension-drf-to-flask"}:
            missing = REQUIRED_PACKAGE_FILES[name] - members
            if missing:
                errors.append(f"{name} wheel is missing required package data: {sorted(missing)}")
            if sorted(requirements) != [
                "sanka-code-migration==0.1.0a3",
                "sanka-drf-replay==0.1.0a4",
                "sanka-extension-sdk==0.1.0a4",
            ]:
                errors.append(f"{name} does not have the exact migration dependency closure")
            if _entry_points(entries, "console_scripts") != {
                name: f"{name.replace('-', '_')}.__main__:main"
            }:
                errors.append(f"{name} wheel has no exact executable entry point")
        else:
            errors.append(f"unexpected release distribution: {name}")
    return errors + _catalog_errors(root, release)


def main(root: Path = ROOT, release: Path = RELEASE) -> int:
    errors = validate_release(root, release)
    if errors:
        print("Release artifact validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Release artifacts: OK ({len(MARKETPLACE_WHEELS)} marketplace wheels; hashes match)")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("dist", nargs="?", type=Path, default=RELEASE)
    raise SystemExit(main(release=parser.parse_args().dist))

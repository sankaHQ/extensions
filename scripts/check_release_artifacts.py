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
)
from scripts.update_marketplace_hashes import MANIFEST_WHEELS, RELEASE_TAG  # noqa: E402

RELEASE = ROOT / "release" / "all"
LOCKED_DEPENDENCY_HASHES = {wheel.name: wheel.sha256 for wheel in LOCKED_DEPENDENCY_WHEELS}
CATALOG: dict[str, Any] = {
    "schema_version": "sanka-marketplace/v1",
    "extensions": [
        {
            "id": "sanka/drf-to-fastapi",
            "manifest": "packages/sanka-extension-drf-to-fastapi/extension.json",
        },
        {
            "id": "sanka/drf-to-flask",
            "manifest": "packages/sanka-extension-drf-to-flask/extension.json",
        },
        {"id": "sanka/markdown", "manifest": "packages/sanka-connector-markdown/extension.json"},
        {"id": "sanka/csv", "manifest": "packages/sanka-connector-csv/extension.json"},
        {"id": "sanka/sqlite", "manifest": "packages/sanka-connector-sqlite/extension.json"},
        {"id": "sanka/postgres", "manifest": "packages/sanka-connector-postgres/extension.json"},
        {
            "id": "sanka/clickhouse",
            "manifest": "packages/sanka-connector-clickhouse/extension.json",
        },
    ],
}
MIGRATION_MANIFEST: dict[str, Any] = {
    "schema_version": "sanka-extension-manifest/v2",
    "kind": "migration",
    "id": "sanka/drf-to-fastapi",
    "version": "0.1.0a3",
    "protocol_version": "sanka-extension/v1",
    "distribution": {
        "name": "sanka-extension-drf-to-fastapi",
        "version": "0.1.0a3",
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
    "runtime": {"sanka_cli": ">=0.2.0,<0.3"},
}
CONNECTOR_MANIFESTS: dict[str, dict[str, Any]] = {
    "sanka-connector-markdown": {
        "schema_version": "sanka-extension-manifest/v2",
        "kind": "connector",
        "id": "sanka/markdown",
        "version": "0.1.0a11",
        "distribution": {
            "name": "sanka-connector-markdown",
            "version": "0.1.0a11",
            "entry_point": "markdown",
        },
        "protocol_version": "sanka-connector/v1",
        "runtime": {"sanka_cli": ">=0.2.0,<0.3"},
        "providers": [{"name": "markdown", "roles": ["source"]}],
    },
    "sanka-connector-csv": {
        "schema_version": "sanka-extension-manifest/v2",
        "kind": "connector",
        "id": "sanka/csv",
        "version": "0.1.0a11",
        "distribution": {
            "name": "sanka-connector-csv",
            "version": "0.1.0a11",
            "entry_point": "csv",
        },
        "protocol_version": "sanka-connector/v1",
        "runtime": {"sanka_cli": ">=0.2.0,<0.3"},
        "providers": [{"name": "csv", "roles": ["source"]}],
    },
    "sanka-connector-sqlite": {
        "schema_version": "sanka-extension-manifest/v2",
        "kind": "connector",
        "id": "sanka/sqlite",
        "version": "0.1.0a11",
        "distribution": {
            "name": "sanka-connector-sqlite",
            "version": "0.1.0a11",
            "entry_point": "sqlite",
        },
        "protocol_version": "sanka-connector/v1",
        "runtime": {"sanka_cli": ">=0.2.0,<0.3"},
        "providers": [{"name": "sqlite", "roles": ["source", "destination"]}],
    },
    "sanka-connector-postgres": {
        "schema_version": "sanka-extension-manifest/v2",
        "kind": "connector",
        "id": "sanka/postgres",
        "version": "0.1.0a11",
        "distribution": {
            "name": "sanka-connector-postgres",
            "version": "0.1.0a11",
            "entry_point": "postgres",
        },
        "protocol_version": "sanka-connector/v1",
        "runtime": {"sanka_cli": ">=0.2.0,<0.3"},
        "providers": [{"name": "postgres", "roles": ["source", "destination"]}],
    },
    "sanka-connector-clickhouse": {
        "schema_version": "sanka-extension-manifest/v2",
        "kind": "connector",
        "id": "sanka/clickhouse",
        "version": "0.1.0a11",
        "distribution": {
            "name": "sanka-connector-clickhouse",
            "version": "0.1.0a11",
            "entry_point": "clickhouse",
        },
        "protocol_version": "sanka-connector/v1",
        "runtime": {"sanka_cli": ">=0.2.0,<0.3"},
        "providers": [{"name": "clickhouse", "roles": ["destination"]}],
    },
}
FLASK_MANIFEST = {
    **MIGRATION_MANIFEST,
    "id": "sanka/drf-to-flask",
    "version": "0.1.0a1",
    "commands": ["apply", "plan", "scan"],
    "targets": ["flask"],
    "distribution": {
        "name": "sanka-extension-drf-to-flask",
        "version": "0.1.0a1",
        "executable": "sanka-extension-drf-to-flask",
    },
}
MANIFESTS = {
    "sanka-extension-drf-to-fastapi": MIGRATION_MANIFEST,
    "sanka-extension-drf-to-flask": FLASK_MANIFEST,
    **CONNECTOR_MANIFESTS,
}
CONNECTOR_ENTRY_POINTS = {
    "sanka-connector-markdown": {"markdown": "sanka_connector_markdown:CONNECTOR"},
    "sanka-connector-csv": {"csv": "sanka_connector_csv:CONNECTOR"},
    "sanka-connector-sqlite": {"sqlite": "sanka_connector_sqlite:CONNECTOR"},
    "sanka-connector-postgres": {"postgres": "sanka_connector_postgres:CONNECTOR"},
    "sanka-connector-clickhouse": {"clickhouse": "sanka_connector_clickhouse:CONNECTOR"},
}


class _EntryPointParser(configparser.ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


def _wheel_metadata(wheel: Path) -> tuple[email.message.Message, str]:
    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = email.message_from_bytes(archive.read(metadata_name))
        entry_name = metadata_name.removesuffix("METADATA") + "entry_points.txt"
        entries = archive.read(entry_name).decode() if entry_name in archive.namelist() else ""
    return metadata, entries


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
            if wheel["url"] != (
                f"https://github.com/sankaHQ/extensions/releases/download/{RELEASE_TAG}/{name}"
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
        try:
            metadata, entries = _wheel_metadata(wheel)
        except (KeyError, StopIteration, zipfile.BadZipFile) as error:
            errors.append(f"invalid wheel metadata in {wheel.name}: {error}")
            continue
        name, version = str(metadata["Name"]), str(metadata["Version"])
        requirements = [
            item.replace(" ", "").lower() for item in metadata.get_all("Requires-Dist", [])
        ]
        if versions.get(name) != version:
            errors.append(f"{name} wheel version {version} does not match package version")
        if name == "sanka-connector-sdk" or name == "sanka-extension-sdk":
            if requirements or entries:
                errors.append(f"{name} SDK wheel must have no dependencies or entry points")
        elif name.startswith("sanka-connector-"):
            connector_entries = _entry_points(entries, "sanka.connectors")
            if f"sanka-connector-sdk=={version}" not in requirements:
                errors.append(f"{name} wheel does not depend on its exact connector SDK")
            if connector_entries != CONNECTOR_ENTRY_POINTS[name]:
                errors.append(f"{name} wheel has no exact connector entry point")
        elif name in {"sanka-extension-drf-to-fastapi", "sanka-extension-drf-to-flask"}:
            if requirements != ["sanka-extension-sdk==0.1.0a1"]:
                errors.append(f"{name} must depend exactly on sanka-extension-sdk==0.1.0a1")
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

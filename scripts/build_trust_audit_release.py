# SPDX-License-Identifier: Apache-2.0
"""Build and validate the separately published, immutable trust-audit closure."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts.build_release import PINNED_LOCAL_WHEELS, download_locked_wheel  # noqa: E402
from scripts.check_release_artifacts import _entry_points, _wheel_metadata  # noqa: E402

PACKAGE = "sanka-extension-trust-audit"
# A release uses the package version; manifest() rejects a template that disagrees.
VERSION: str = tomllib.loads((ROOT / "packages" / PACKAGE / "pyproject.toml").read_text())[
    "project"
]["version"]
TAG = f"trust-audit-v{VERSION}"
PREFIX = f"https://github.com/sankaHQ/extensions/releases/download/{TAG}/"
WHEEL = f"sanka_extension_trust_audit-{VERSION}-py3-none-any.whl"
MANIFEST_NAME = f"{PACKAGE}.json"
# Hatchling's deterministic timestamp, so the reviewed manifest hash is reproducible.
SOURCE_DATE_EPOCH = "1580601600"
SDK_WHEELS = tuple(
    wheel
    for wheel in PINNED_LOCAL_WHEELS
    if wheel.distribution in {"sanka-extension-sdk", "sanka-connector-sdk"}
)
CATALOG = {
    "schema_version": "sanka-marketplace/v1",
    "extensions": [{"id": "sanka/trust-audit", "manifest": MANIFEST_NAME}],
}


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def manifest(root: Path = ROOT) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(
        (root / "packages" / PACKAGE / "extension.json").read_text()
    )
    template = json.loads((root / "packages" / PACKAGE / "extension.template.json").read_text())
    if result | {"wheels": []} != template:
        raise ValueError("Trust audit manifest differs from the reviewed extension contract")
    if {result.get("version"), result.get("distribution", {}).get("version")} != {VERSION}:
        raise ValueError(f"Trust audit manifest and template must declare version {VERSION}")
    expected = [WHEEL, *(wheel.name for wheel in SDK_WHEELS)]
    entries = result.get("wheels")
    if not isinstance(entries, list) or [item.get("name") for item in entries] != expected:
        raise ValueError("Trust audit manifest must contain the exact three-wheel closure")
    pinned = {wheel.name: wheel.sha256 for wheel in SDK_WHEELS}
    for item in entries:
        sha = item.get("sha256", "")
        if (
            set(item) != {"name", "url", "sha256"}
            or item["url"] != PREFIX + item["name"]
            or len(sha) != 64
            or any(char not in "0123456789abcdef" for char in sha)
            or (item["name"] in pinned and sha != pinned[item["name"]])
        ):
            raise ValueError("Trust audit manifest has an invalid immutable URL or wheel hash")
    return result


def validate(output: Path, root: Path = ROOT) -> None:
    expected = manifest(root)
    names = {item["name"] for item in expected["wheels"]} | {MANIFEST_NAME, "marketplace.json"}
    if {path.name for path in output.iterdir()} != names:
        raise ValueError("Trust audit release has missing or unexpected assets")
    for name in names:
        if (output / name).is_symlink() or not (output / name).is_file():
            raise ValueError("Trust audit release assets must be regular files")
    if json.loads((output / MANIFEST_NAME).read_text()) != expected:
        raise ValueError("Trust audit release manifest differs from reviewed source")
    if json.loads((output / "marketplace.json").read_text()) != CATALOG:
        raise ValueError("Trust audit release catalog differs from the scoped catalog")
    for item in expected["wheels"]:
        if digest(output / item["name"]) != item["sha256"]:
            raise ValueError(f"Trust audit release wheel hash mismatch: {item['name']}")
    metadata, entries, members = _wheel_metadata(output / WHEEL)
    requirements = [item.replace(" ", "") for item in metadata.get_all("Requires-Dist", [])]
    if (
        metadata["Name"] != PACKAGE
        or metadata["Version"] != VERSION
        or requirements != ["sanka-extension-sdk==0.1.0a4"]
        or _entry_points(entries, "console_scripts")
        != {PACKAGE: "sanka_extension_trust_audit.__main__:main"}
    ):
        raise ValueError("Trust audit wheel metadata/dependencies/entry point changed")
    if "sanka_extension_trust_audit/sanka-extension-settings.json" not in members:
        raise ValueError("Trust audit wheel is missing its plan settings declaration")


def build_wheel(output: Path) -> Path:
    subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--package",
            PACKAGE,
            "--out-dir",
            str(output),
            "--no-create-gitignore",
        ],
        cwd=ROOT,
        env=os.environ | {"SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH},
        check=True,
    )
    return output / WHEEL


def write_manifest(output: Path) -> None:
    """Rewrite extension.json from the template and a freshly built wheel."""
    template = json.loads((ROOT / "packages" / PACKAGE / "extension.template.json").read_text())
    wheel = build_wheel(output)
    template["wheels"] = [
        {"name": WHEEL, "sha256": digest(wheel), "url": PREFIX + WHEEL},
        *({"name": w.name, "sha256": w.sha256, "url": PREFIX + w.name} for w in SDK_WHEELS),
    ]
    path = ROOT / "packages" / PACKAGE / "extension.json"
    path.write_text(json.dumps(template, indent=2, sort_keys=True) + "\n")


def _release_dir(output: Path) -> Path:
    # Restrict cleanup/writes to a dedicated repository-owned release directory.
    absolute = output.absolute()
    if any(path.is_symlink() for path in (absolute, *absolute.parents)):
        raise ValueError("Trust audit release output must not traverse symlinks")
    resolved = absolute.resolve()
    if not resolved.is_relative_to(ROOT / "release") or resolved == ROOT / "release":
        raise ValueError("Trust audit release output must be below repository release/")
    return resolved


def build(output: Path) -> None:
    output = _release_dir(output)
    expected = manifest()
    output.mkdir(parents=True, exist_ok=True)
    names = {item["name"] for item in expected["wheels"]} | {MANIFEST_NAME, "marketplace.json"}
    if any(path.name not in names or path.is_symlink() for path in output.iterdir()):
        raise ValueError("Trust audit output contains unexpected files or symlinks")
    build_wheel(output)
    for wheel in SDK_WHEELS:
        download_locked_wheel(output, wheel)
    (output / MANIFEST_NAME).write_text(json.dumps(expected, indent=2) + "\n")
    (output / "marketplace.json").write_text(json.dumps(CATALOG, indent=2) + "\n")
    validate(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "release" / "trust-audit")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--write-manifest", action="store_true")
    args = parser.parse_args()
    if args.write_manifest:
        output = _release_dir(args.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        write_manifest(output)
    if args.check_only:
        validate(args.output_dir)
    else:
        build(args.output_dir)
    print(f"Trust audit release verified: {TAG}; three immutable wheels and scoped catalog")


if __name__ == "__main__":
    main()

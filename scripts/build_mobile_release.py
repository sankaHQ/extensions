# SPDX-License-Identifier: Apache-2.0
"""Build the scoped, reproducible React Native experimental release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts.build_api_release import PACKAGES as API_PACKAGES  # noqa: E402
from scripts.build_api_release import SDK, TS_DIGEST, digest  # noqa: E402
from scripts.build_api_release import manifest as api_manifest  # noqa: E402
from scripts.build_release import download_locked_wheel  # noqa: E402
from scripts.check_release_artifacts import _entry_points, _wheel_metadata  # noqa: E402

VERSION = "0.1.0a1"
TAG = f"mobile-converters-v{VERSION}"
PREFIX = f"https://github.com/sankaHQ/extensions/releases/download/{TAG}/"
PACKAGES = ("sanka-extension-react-native-to-native", "sanka-ts-capture")
DEPENDENCIES = {
    PACKAGES[0]: ["sanka-extension-sdk==0.1.0a4", "sanka-ts-capture==0.1.0a1"],
    PACKAGES[1]: [],
}
REPLAY_HARNESS = ("node/rn-tree-run.js", "node/react-native-stub.js")
CATALOG = {
    "schema_version": "sanka-marketplace/v1",
    "extensions": [
        {"id": "sanka/" + p.removeprefix("sanka-extension-"), "manifest": f"{p}.json"}
        for p in PACKAGES[:1]
    ],
}


def wheel_name(package: str) -> str:
    return f"{package.replace('-', '_')}-{VERSION}-py3-none-any.whl"


CLOSURE = [*(wheel_name(p) for p in PACKAGES), *(w.name for w in SDK)]


def published_digests() -> dict[str, str]:
    """Wheels shared with the Go/Rust release keep the bytes published under its tag."""
    shared = {w.name: w.sha256 for w in SDK}
    for wheel in api_manifest(API_PACKAGES[1])["wheels"]:
        if wheel["name"] == wheel_name(PACKAGES[1]):
            shared[wheel["name"]] = wheel["sha256"]
    return shared


def manifest(package: str, root: Path = ROOT) -> dict[str, Any]:
    directory = root / "packages" / package
    result: dict[str, Any] = json.loads((directory / "extension.json").read_text())
    template = json.loads((directory / "extension.template.json").read_text())
    if result | {"wheels": []} != template:
        raise ValueError(f"{package}: manifest contract differs from template")
    entries = result.get("wheels", [])
    if [w.get("name") for w in entries] != CLOSURE:
        raise ValueError(f"{package}: incomplete wheel closure")
    published = published_digests()
    for wheel in entries:
        sha = wheel.get("sha256", "")
        if (
            set(wheel) != {"name", "url", "sha256"}
            or wheel["url"] != PREFIX + wheel["name"]
            or len(sha) != 64
            or any(c not in "0123456789abcdef" for c in sha)
        ):
            raise ValueError(f"{package}: invalid URL or digest")
        if wheel["name"] in published and sha != published[wheel["name"]]:
            raise ValueError(f"{package}: {wheel['name']} differs from its published bytes")
    return result


def validate(output: Path, root: Path = ROOT) -> None:
    data = manifest(PACKAGES[0], root)
    assets = set(CLOSURE) | {f"{PACKAGES[0]}.json", "marketplace.json"}
    if {p.name for p in output.iterdir()} != assets:
        raise ValueError("Missing or unexpected release assets")
    if any((output / n).is_symlink() or not (output / n).is_file() for n in assets):
        raise ValueError("Release assets must be regular files")
    if json.loads((output / "marketplace.json").read_text()) != CATALOG:
        raise ValueError("Scoped release catalog changed")
    if json.loads((output / f"{PACKAGES[0]}.json").read_text()) != data:
        raise ValueError("Release manifest differs from reviewed source")
    for wheel in data["wheels"]:
        if digest(output / wheel["name"]) != wheel["sha256"]:
            raise ValueError(f"Wheel digest mismatch: {wheel['name']}")
    for package in PACKAGES:
        metadata, entries, members = _wheel_metadata(output / wheel_name(package))
        requirements = sorted(x.replace(" ", "") for x in metadata.get_all("Requires-Dist", []))
        expected_entry = {package: package.replace("-", "_") + ".__main__:main"}
        if (
            metadata["Name"] != package
            or metadata["Version"] != VERSION
            or requirements != DEPENDENCIES[package]
            or _entry_points(entries, "console_scripts")
            != (expected_entry if package == PACKAGES[0] else {})
        ):
            raise ValueError(f"Wheel metadata, dependencies or executable changed: {package}")
        module = package.replace("-", "_")
        harness = {f"{module}/{name}" for name in REPLAY_HARNESS}
        if package == PACKAGES[0] and not harness <= members:
            raise ValueError("React Native wheel missing its replay harness")
        if package == PACKAGES[1]:
            with zipfile.ZipFile(output / wheel_name(package)) as archive:
                if (
                    hashlib.sha256(archive.read(f"{module}/node/typescript.js")).hexdigest()
                    != TS_DIGEST
                ):
                    raise ValueError("TypeScript compiler bundle digest changed")
                if f"{module}/node/LICENSE.typescript.txt" not in members:
                    raise ValueError("TypeScript license absent")


def build(output: Path, *, write_manifests: bool = False) -> None:
    absolute = output.absolute()
    if any(p.is_symlink() for p in (absolute, *absolute.parents)):
        raise ValueError("Release output must not traverse symlinks")
    output = absolute.resolve()
    if not output.is_relative_to(ROOT / "release") or output == ROOT / "release":
        raise ValueError("Output must be below repository release/")
    output.mkdir(parents=True, exist_ok=True)
    allowed = set(CLOSURE) | {f"{PACKAGES[0]}.json", "marketplace.json"}
    if any(p.name not in allowed or p.is_symlink() or not p.is_file() for p in output.iterdir()):
        raise ValueError("Release output contains unexpected files or symlinks")
    env = os.environ | {"SOURCE_DATE_EPOCH": "315532800"}
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/fetch_typescript_bundle.py")],
        cwd=ROOT,
        env=env,
        check=True,
    )
    for package in PACKAGES:
        subprocess.run(
            [
                "uv",
                "build",
                "--wheel",
                "--package",
                package,
                "--build-constraint",
                str(ROOT / "scripts/api-build-constraints.txt"),
                "--out-dir",
                str(output),
                "--no-create-gitignore",
            ],
            cwd=ROOT,
            env=env,
            check=True,
        )
    for wheel in SDK:
        download_locked_wheel(output, wheel)
    for package in PACKAGES[:1]:
        if write_manifests:
            directory = ROOT / "packages" / package
            data = json.loads((directory / "extension.template.json").read_text())
            data["wheels"] = [
                {"name": n, "url": PREFIX + n, "sha256": digest(output / n)} for n in CLOSURE
            ]
            (directory / "extension.json").write_text(json.dumps(data, indent=2) + "\n")
        (output / f"{package}.json").write_text(json.dumps(manifest(package), indent=2) + "\n")
    (output / "marketplace.json").write_text(json.dumps(CATALOG, indent=2) + "\n")
    validate(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "release/mobile-converters")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--write-manifests", action="store_true")
    args = parser.parse_args()
    if args.check_only:
        validate(args.output_dir)
    else:
        build(args.output_dir, write_manifests=args.write_manifests)
    print(f"Validated {TAG}: four wheels, one manifest and scoped catalog")


if __name__ == "__main__":
    main()

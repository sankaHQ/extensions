# SPDX-License-Identifier: Apache-2.0
"""Build a scoped candidate bundle; never publish or alter released manifests."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts.build_release import (  # noqa: E402
    PINNED_LOCAL_WHEELS,
    download_locked_wheel,
)

PACKAGES = (
    "sanka-http-replay",
    "sanka-java-migration",
    "sanka-extension-java-to-python",
)
PREFIX = "https://github.com/sankaHQ/extensions/releases/download/java-converters-v0.1.0a1/"


def source_revision() -> str:
    status = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT, text=True
    )
    if status.strip():
        raise ValueError("Java candidate bundles require a clean, committed source checkout")
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def build() -> None:
    output = ROOT / "release/java-converters"
    if output.is_symlink():
        raise ValueError("release output must be a repository-owned directory")
    output.mkdir(parents=True, exist_ok=True)
    provenance = output / "build-provenance.json"
    provenance.unlink(missing_ok=True)
    revision = source_revision()
    wheels = []
    for package in PACKAGES:
        project = tomllib.loads((ROOT / "packages" / package / "pyproject.toml").read_text())[
            "project"
        ]
        subprocess.run(
            [
                "uv",
                "build",
                "--wheel",
                "--package",
                package,
                "--out-dir",
                str(output),
                "--no-create-gitignore",
            ],
            cwd=ROOT,
            env=os.environ | {"SOURCE_DATE_EPOCH": "315532800"},
            check=True,
        )
        wheels.append(output / f"{package.replace('-', '_')}-{project['version']}-py3-none-any.whl")
    dependencies = tuple(
        w
        for w in PINNED_LOCAL_WHEELS
        if w.distribution in {"sanka-extension-sdk", "sanka-connector-sdk"}
    )
    wheels.extend(download_locked_wheel(output, wheel) for wheel in dependencies)
    # A complete, immutable closure is installable without an implicit PyPI fallback.
    entries = [
        {
            "name": wheel.name,
            "url": PREFIX + wheel.name,
            "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        }
        for wheel in wheels
    ]
    artifacts = list(wheels)
    for package in PACKAGES[-1:]:
        manifest = json.loads((ROOT / "packages" / package / "extension.template.json").read_text())
        manifest["wheels"] = entries
        manifest_path = output / (package + ".json")
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        artifacts.append(manifest_path)
    catalog = {
        "schema_version": "sanka-marketplace/v1",
        "extensions": [
            {
                "id": "sanka/" + package.removeprefix("sanka-extension-"),
                "manifest": package + ".json",
            }
            for package in PACKAGES[-1:]
        ],
    }
    (output / "marketplace.json").write_text(json.dumps(catalog, indent=2) + "\n")
    artifacts.append(output / "marketplace.json")
    with zipfile.ZipFile(
        next(p for p in wheels if p.name.startswith("sanka_java_migration-"))
    ) as wheel:
        parser = wheel.read("sanka_java_migration/java/javaparser-core.jar")
        if (
            hashlib.sha256(parser).hexdigest()
            != "b5499a3b1c40b16c0671fabe478c9aafeab38160c6fde74a6c13f42d86716ecd"
        ):
            raise ValueError("JavaParser is missing or changed inside the installed wheel")
        wheel.read("sanka_java_migration/java/Analyzer.java")
        wheel.read("sanka_java_migration/java/ReplayTest.java.txt")
    with zipfile.ZipFile(
        next(p for p in wheels if p.name.startswith("sanka_extension_java_to_python-"))
    ) as wheel:
        wheel.read("sanka_extension_java_to_python/requirements.lock")
        wheel.read("sanka_extension_java_to_python/runners/observe.py.txt")
    if source_revision() != revision:
        raise ValueError("Java candidate source changed during the build")
    provenance.write_text(
        json.dumps(
            {
                "schema": "sanka.java-candidate-build/v1",
                "revision": revision,
                "artifacts": {
                    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted(artifacts)
                },
            },
            indent=2,
        )
        + "\n"
    )
    print(f"Built {len(wheels)} candidate wheels and one manifest in {output}")


if __name__ == "__main__":
    build()

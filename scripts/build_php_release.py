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
    locked_dependency_wheels,
)

PACKAGES = (
    "sanka-code-migration",
    "sanka-http-replay",
    "sanka-php-migration",
    "sanka-extension-php-to-fastapi",
    "sanka-extension-php-to-golang",
)
PREFIX = "https://github.com/sankaHQ/extensions/releases/download/php-converters-v0.1.0a1/"


def source_revision() -> str:
    status = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT, text=True
    )
    if status.strip():
        raise ValueError("PHP candidate bundles require a clean, committed source checkout")
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def build() -> None:
    output = ROOT / "release/php-converters"
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
    dependencies += locked_dependency_wheels(dependencies=("tree-sitter", "tree-sitter-php"))
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
    for package in PACKAGES[-2:]:
        manifest = json.loads((ROOT / "packages" / package / "extension.template.json").read_text())
        other = next(name for name in PACKAGES[-2:] if name != package).replace("-", "_")
        manifest["wheels"] = [item for item in entries if not item["name"].startswith(other)]
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
            for package in PACKAGES[-2:]
        ],
    }
    (output / "marketplace.json").write_text(json.dumps(catalog, indent=2) + "\n")
    artifacts.append(output / "marketplace.json")
    with zipfile.ZipFile(
        next(p for p in wheels if p.name.startswith("sanka_php_migration-"))
    ) as wheel:
        if not any(name.endswith("runners/source.php") for name in wheel.namelist()):
            raise ValueError("PHP source replay runner missing from wheel")
        if not any(name.endswith("locks/laravel/composer.lock") for name in wheel.namelist()):
            raise ValueError("qualified Laravel dependency lock missing from wheel")
    if source_revision() != revision:
        raise ValueError("PHP candidate source changed during the build")
    provenance.write_text(
        json.dumps(
            {
                "schema": "sanka.php-candidate-build/v1",
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
    print(f"Built {len(wheels)} candidate wheels and two manifests in {output}")


if __name__ == "__main__":
    build()

# SPDX-License-Identifier: Apache-2.0
"""Build every publishable workspace package into one reviewed release set."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "release" / "all"


def _distribution_artifacts(directory: Path) -> list[Path]:
    return sorted(
        path
        for path in directory.iterdir()
        if path.suffix == ".whl" or path.name.endswith(".tar.gz")
    )


def main() -> None:
    release_root = RELEASE.parent
    if release_root.exists():
        shutil.rmtree(release_root)
    RELEASE.mkdir(parents=True)
    packages = sorted(path.name for path in (ROOT / "packages").iterdir() if path.is_dir())
    extension_packages = ["sanka-extension-sdk", "sanka-extension-drf-to-fastapi"]
    packages = [package for package in packages if package not in extension_packages]
    packages.extend(extension_packages)
    build_environment = os.environ.copy()
    build_environment["SOURCE_DATE_EPOCH"] = "315532800"
    for package in packages:
        package_release = release_root / "packages" / package
        package_release.mkdir(parents=True)
        subprocess.run(
            [
                "uv",
                "build",
                "--package",
                package,
                "--out-dir",
                str(package_release),
                "--no-create-gitignore",
            ],
            cwd=ROOT,
            env=build_environment,
            check=True,
        )
        artifacts = _distribution_artifacts(package_release)
        if len(artifacts) != 2:
            names = ", ".join(path.name for path in artifacts) or "none"
            raise RuntimeError(f"{package}: expected wheel and sdist, found {names}")
        for artifact in artifacts:
            shutil.copy2(artifact, RELEASE / artifact.name)
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (release_root / "SOURCE_COMMIT").write_text(source_commit + "\n", encoding="utf-8")
    hashes = []
    for artifact in _distribution_artifacts(RELEASE):
        with artifact.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        hashes.append(f"{digest}  {artifact.name}")
    (release_root / "SHA256SUMS").write_text("\n".join(hashes) + "\n", encoding="utf-8")
    print(f"Built {len(packages)} connector and extension distributions in {RELEASE}")


if __name__ == "__main__":
    main()

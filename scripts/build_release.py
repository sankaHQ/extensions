# SPDX-License-Identifier: Apache-2.0
"""Build every publishable workspace package into one reviewed release set."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "release" / "all"


def main() -> None:
    release_root = RELEASE.parent
    if release_root.exists():
        shutil.rmtree(release_root)
    RELEASE.mkdir(parents=True)
    packages = sorted(path.name for path in (ROOT / "packages").iterdir() if path.is_dir())
    for package in packages:
        package_release = release_root / "packages" / package
        package_release.mkdir(parents=True)
        subprocess.run(
            ["uv", "build", "--package", package, "--out-dir", str(package_release)],
            cwd=ROOT,
            check=True,
        )
        for artifact in package_release.iterdir():
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
    for artifact in sorted(RELEASE.iterdir()):
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        hashes.append(f"{digest}  {artifact.name}")
    (release_root / "SHA256SUMS").write_text("\n".join(hashes) + "\n", encoding="utf-8")
    print(f"Built {len(packages)} connector distributions in {RELEASE}")


if __name__ == "__main__":
    main()

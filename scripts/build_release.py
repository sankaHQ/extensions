# SPDX-License-Identifier: Apache-2.0
"""Build every publishable workspace package into one reviewed release set."""

from __future__ import annotations

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
        subprocess.run(
            ["uv", "build", "--package", package, "--out-dir", str(RELEASE)],
            cwd=ROOT,
            check=True,
        )
    print(f"Built {len(packages)} connector distributions in {RELEASE}")


if __name__ == "__main__":
    main()

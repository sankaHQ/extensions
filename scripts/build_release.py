# SPDX-License-Identifier: Apache-2.0
"""Build the exact wheel set served by the GitHub marketplace release."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKETPLACE_PACKAGES = (
    "sanka-extension-sdk",
    "sanka-extension-drf-to-fastapi",
    "sanka-connector-sdk",
    "sanka-connector-markdown",
    "sanka-connector-csv",
    "sanka-connector-sqlite",
    "sanka-connector-postgres",
    "sanka-connector-clickhouse",
)


def build(output_dir: Path) -> None:
    output_dir = output_dir.resolve()
    if output_dir == ROOT:
        raise ValueError("release output directory cannot be the repository root")
    shutil.rmtree(output_dir, ignore_errors=True)
    output_dir.mkdir(parents=True)
    environment = os.environ | {"SOURCE_DATE_EPOCH": "315532800"}
    for package in MARKETPLACE_PACKAGES:
        subprocess.run(
            [
                "uv",
                "build",
                "--wheel",
                "--package",
                package,
                "--out-dir",
                str(output_dir),
                "--no-create-gitignore",
            ],
            cwd=ROOT,
            env=environment,
            check=True,
        )
    print(f"Built {len(MARKETPLACE_PACKAGES)} marketplace wheels in {output_dir}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "release" / "all")
    args = parser.parse_args()
    build(args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

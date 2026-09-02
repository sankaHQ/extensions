# SPDX-License-Identifier: Apache-2.0
"""Build the exact wheel set served by the GitHub marketplace release."""

from __future__ import annotations

import argparse
import os
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
MARKETPLACE_WHEELS = (
    "sanka_extension_sdk-0.1.0a1-py3-none-any.whl",
    "sanka_extension_drf_to_fastapi-0.1.0a1-py3-none-any.whl",
    "sanka_connector_sdk-0.1.0a11-py3-none-any.whl",
    "sanka_connector_markdown-0.1.0a11-py3-none-any.whl",
    "sanka_connector_csv-0.1.0a11-py3-none-any.whl",
    "sanka_connector_sqlite-0.1.0a11-py3-none-any.whl",
    "sanka_connector_postgres-0.1.0a11-py3-none-any.whl",
    "sanka_connector_clickhouse-0.1.0a11-py3-none-any.whl",
)


def _prepare_output(output_dir: Path, *, root: Path = ROOT) -> Path:
    root = root.resolve()
    output_dir = output_dir.resolve()
    try:
        relative = output_dir.relative_to(root)
    except ValueError as error:
        raise ValueError("release output directory must be repository-owned") from error
    if not relative.parts or relative.parts[0] not in {"dist", "release"}:
        raise ValueError("release output directory must be repository-owned")
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in MARKETPLACE_WHEELS:
        (output_dir / name).unlink(missing_ok=True)
    return output_dir


def build(output_dir: Path) -> None:
    output_dir = _prepare_output(output_dir)
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

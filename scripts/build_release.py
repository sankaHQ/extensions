# SPDX-License-Identifier: Apache-2.0
"""Build the exact wheel set served by the GitHub marketplace release."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
MAX_DEPENDENCY_WHEEL_BYTES = 128 * 1024 * 1024
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
LOCAL_WHEELS = (
    "sanka_extension_sdk-0.1.0a1-py3-none-any.whl",
    "sanka_extension_drf_to_fastapi-0.1.0a3-py3-none-any.whl",
    "sanka_connector_sdk-0.1.0a11-py3-none-any.whl",
    "sanka_connector_markdown-0.1.0a11-py3-none-any.whl",
    "sanka_connector_csv-0.1.0a11-py3-none-any.whl",
    "sanka_connector_sqlite-0.1.0a11-py3-none-any.whl",
    "sanka_connector_postgres-0.1.0a11-py3-none-any.whl",
    "sanka_connector_clickhouse-0.1.0a11-py3-none-any.whl",
)
DEPENDENCIES = (
    "backports-zstd",
    "certifi",
    "clickhouse-connect",
    "lz4",
    "psycopg",
    "psycopg-binary",
    "pyyaml",
    "tzdata",
    "typing-extensions",
    "urllib3",
)


@dataclass(frozen=True)
class LockedWheel:
    distribution: str
    name: str
    url: str
    sha256: str
    size: int


def locked_dependency_wheels(*, root: Path = ROOT) -> tuple[LockedWheel, ...]:
    packages = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))["package"]
    locked: list[LockedWheel] = []
    for distribution in DEPENDENCIES:
        matches = [package for package in packages if package["name"] == distribution]
        if len(matches) != 1 or not matches[0].get("wheels"):
            raise RuntimeError(f"uv.lock has no unique wheel set for {distribution}")
        for wheel in matches[0]["wheels"]:
            url = wheel["url"]
            parsed = urlparse(url)
            digest = wheel["hash"].removeprefix("sha256:")
            size = wheel.get("size")
            name = Path(unquote(parsed.path)).name
            if (
                parsed.scheme != "https"
                or parsed.hostname != "files.pythonhosted.org"
                or not name.endswith(".whl")
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or type(size) is not int
                or not 0 < size <= MAX_DEPENDENCY_WHEEL_BYTES
            ):
                raise RuntimeError(f"uv.lock has an invalid wheel for {distribution}")
            locked.append(LockedWheel(distribution, name, url, digest, size))
    names = [wheel.name for wheel in locked]
    if len(names) != len(set(names)):
        raise RuntimeError("uv.lock contains duplicate dependency wheel filenames")
    return tuple(sorted(locked, key=lambda wheel: (wheel.distribution, wheel.name)))


LOCKED_DEPENDENCY_WHEELS = locked_dependency_wheels()
MARKETPLACE_WHEELS = LOCAL_WHEELS + tuple(wheel.name for wheel in LOCKED_DEPENDENCY_WHEELS)


def download_locked_wheel(output_dir: Path, wheel: LockedWheel) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with (
            urlopen(wheel.url, timeout=30) as response,
            tempfile.NamedTemporaryFile(dir=output_dir, delete=False) as handle,
        ):
            temporary = Path(handle.name)
            digest = hashlib.sha256()
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > wheel.size:
                    raise RuntimeError(f"locked size exceeded for {wheel.name}")
                digest.update(chunk)
                handle.write(chunk)
        if size != wheel.size:
            raise RuntimeError(f"locked size mismatch for {wheel.name}")
        if digest.hexdigest() != wheel.sha256:
            raise RuntimeError(f"SHA-256 mismatch for {wheel.name}")
        destination = output_dir / wheel.name
        os.replace(temporary, destination)
        temporary = None
        return destination
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
    for wheel in LOCKED_DEPENDENCY_WHEELS:
        download_locked_wheel(output_dir, wheel)
    print(f"Built {len(MARKETPLACE_WHEELS)} marketplace wheels in {output_dir}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "release" / "all")
    args = parser.parse_args()
    build(args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
MARKETPLACE_PACKAGES: tuple[str, ...] = ("sanka-extension-drf-to-fastapi",)
LOCAL_WHEELS = (
    "sanka_drf_replay-0.1.0a4-py3-none-any.whl",
    "sanka_code_migration-0.1.0a3-py3-none-any.whl",
    "sanka_extension_sdk-0.1.0a4-py3-none-any.whl",
    "sanka_extension_drf_to_fastapi-0.1.0a18-py3-none-any.whl",
    "sanka_extension_drf_to_flask-0.1.0a12-py3-none-any.whl",
    "sanka_connector_sdk-0.1.0a12-py3-none-any.whl",
    "sanka_connector_markdown-0.1.0a14-py3-none-any.whl",
    "sanka_connector_csv-0.1.0a14-py3-none-any.whl",
    "sanka_connector_sqlite-0.1.0a14-py3-none-any.whl",
    "sanka_connector_postgres-0.1.0a14-py3-none-any.whl",
    "sanka_connector_clickhouse-0.1.0a14-py3-none-any.whl",
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


# Existing marketplace extensions still pin the published a4 SDK. Build the new
# SDK candidate separately; never substitute new bytes under their old filename.
PINNED_EXTENSION_SDK = LockedWheel(
    "sanka-extension-sdk",
    "sanka_extension_sdk-0.1.0a4-py3-none-any.whl",
    "https://github.com/sankaHQ/extensions/releases/download/sdk-v0.1.0a4/"
    "sanka_extension_sdk-0.1.0a4-py3-none-any.whl",
    "f1a6655095ab81e549137e1d9604492bda2a31677d674c6359b0a39a2da96307",
    46367,
)
PINNED_LOCAL_WHEELS = (
    PINNED_EXTENSION_SDK,
    # These a31 assets are published; rebuilding changed source under the same
    # wheel filenames would make the catalog point at bytes users cannot fetch.
    LockedWheel(
        "sanka-drf-replay",
        "sanka_drf_replay-0.1.0a4-py3-none-any.whl",
        "https://github.com/sankaHQ/extensions/releases/download/extensions-v0.1.0a31/"
        "sanka_drf_replay-0.1.0a4-py3-none-any.whl",
        "a60f2124202efe75b10c5c71e6dd3681a77cdd8fbb3568424bd8c18362e5c2bf",
        26608,
    ),
    LockedWheel(
        "sanka-code-migration",
        "sanka_code_migration-0.1.0a3-py3-none-any.whl",
        "https://github.com/sankaHQ/extensions/releases/download/extensions-v0.1.0a31/"
        "sanka_code_migration-0.1.0a3-py3-none-any.whl",
        "e510b981e5ddcad3a269106a69565d034486abed275aaf4217537c6b6b1efe11",
        75727,
    ),
    LockedWheel(
        "sanka-extension-drf-to-flask",
        "sanka_extension_drf_to_flask-0.1.0a12-py3-none-any.whl",
        "https://github.com/sankaHQ/extensions/releases/download/extensions-v0.1.0a31/"
        "sanka_extension_drf_to_flask-0.1.0a12-py3-none-any.whl",
        "1bd9cc1d1d38a49bd58a7578afe8313914c02d4508bacf0680ba101420b5e891",
        119228,
    ),
    LockedWheel(
        "sanka-connector-sdk",
        "sanka_connector_sdk-0.1.0a12-py3-none-any.whl",
        "https://github.com/sankaHQ/extensions/releases/download/extensions-v0.1.0a25/"
        "sanka_connector_sdk-0.1.0a12-py3-none-any.whl",
        "34da5c35aaa60fc19258e76b72a3eca58bf52fff96e2ccf9a0aa1115f8878d8e",
        17355,
    ),
    LockedWheel(
        "sanka-connector-markdown",
        "sanka_connector_markdown-0.1.0a14-py3-none-any.whl",
        "https://github.com/sankaHQ/extensions/releases/download/extensions-v0.1.0a25/"
        "sanka_connector_markdown-0.1.0a14-py3-none-any.whl",
        "702ab178a936849a3ba8781e58ba2ffa14b4857c5a0778f64d961f3256e91579",
        9751,
    ),
    LockedWheel(
        "sanka-connector-csv",
        "sanka_connector_csv-0.1.0a14-py3-none-any.whl",
        "https://github.com/sankaHQ/extensions/releases/download/extensions-v0.1.0a25/"
        "sanka_connector_csv-0.1.0a14-py3-none-any.whl",
        "8980178f7ab1da0c32da561a5e59e963606328bbab2fbdb50d02bdadb883b8ef",
        8952,
    ),
    LockedWheel(
        "sanka-connector-sqlite",
        "sanka_connector_sqlite-0.1.0a14-py3-none-any.whl",
        "https://github.com/sankaHQ/extensions/releases/download/extensions-v0.1.0a25/"
        "sanka_connector_sqlite-0.1.0a14-py3-none-any.whl",
        "b6437e9005d5c44f14b1e1eea52d6e222f5c6396a1b245532751e3144e52d3eb",
        10967,
    ),
    LockedWheel(
        "sanka-connector-postgres",
        "sanka_connector_postgres-0.1.0a14-py3-none-any.whl",
        "https://github.com/sankaHQ/extensions/releases/download/extensions-v0.1.0a25/"
        "sanka_connector_postgres-0.1.0a14-py3-none-any.whl",
        "a8f3c796f7c8c8eb47f264f39ce1dcda213d6323ddaa6ec77b9d79007a26dcc1",
        20059,
    ),
    LockedWheel(
        "sanka-connector-clickhouse",
        "sanka_connector_clickhouse-0.1.0a14-py3-none-any.whl",
        "https://github.com/sankaHQ/extensions/releases/download/extensions-v0.1.0a25/"
        "sanka_connector_clickhouse-0.1.0a14-py3-none-any.whl",
        "d9173413e5f63fd6123a23136085a60ea07b64b0f4a5a2f9b7bd144659d772aa",
        12856,
    ),
)

LOCKED_DEPENDENCY_WHEELS = locked_dependency_wheels()
MARKETPLACE_WHEELS = LOCAL_WHEELS + tuple(wheel.name for wheel in LOCKED_DEPENDENCY_WHEELS)


def download_locked_wheel(output_dir: Path, wheel: LockedWheel) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / wheel.name
    # Reuse only immutable published bytes, verified on every invocation.
    # Failed downloads cannot enter this cache.
    if (
        not destination.is_symlink()
        and destination.is_file()
        and destination.stat().st_size == wheel.size
    ):
        with destination.open("rb") as cached:
            if hashlib.file_digest(cached, "sha256").hexdigest() == wheel.sha256:
                return destination
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
    pinned = {wheel.name for wheel in PINNED_LOCAL_WHEELS}
    for name in LOCAL_WHEELS:
        if name in pinned:
            continue
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
    for wheel in (*PINNED_LOCAL_WHEELS, *LOCKED_DEPENDENCY_WHEELS):
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

# SPDX-License-Identifier: Apache-2.0
"""Record immutable GitHub wheel hashes in every marketplace manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:  # Direct script execution keeps only scripts/ on sys.path.
    sys.path.insert(0, str(ROOT))

from scripts.build_release import LOCKED_DEPENDENCY_WHEELS  # noqa: E402

RELEASE_TAG = "extensions-v0.1.0a11"
LOCAL_MANIFEST_WHEELS = {
    "sanka-extension-drf-to-fastapi": (
        "sanka_extension_sdk-0.1.0a1-py3-none-any.whl",
        "sanka_extension_drf_to_fastapi-0.1.0a1-py3-none-any.whl",
    ),
    "sanka-connector-markdown": (
        "sanka_connector_sdk-0.1.0a11-py3-none-any.whl",
        "sanka_connector_markdown-0.1.0a11-py3-none-any.whl",
    ),
    "sanka-connector-csv": (
        "sanka_connector_sdk-0.1.0a11-py3-none-any.whl",
        "sanka_connector_csv-0.1.0a11-py3-none-any.whl",
    ),
    "sanka-connector-sqlite": (
        "sanka_connector_sdk-0.1.0a11-py3-none-any.whl",
        "sanka_connector_sqlite-0.1.0a11-py3-none-any.whl",
    ),
    "sanka-connector-postgres": (
        "sanka_connector_sdk-0.1.0a11-py3-none-any.whl",
        "sanka_connector_postgres-0.1.0a11-py3-none-any.whl",
    ),
    "sanka-connector-clickhouse": (
        "sanka_connector_sdk-0.1.0a11-py3-none-any.whl",
        "sanka_connector_clickhouse-0.1.0a11-py3-none-any.whl",
    ),
}
MANIFEST_DEPENDENCIES = {
    "sanka-extension-drf-to-fastapi": (),
    "sanka-connector-markdown": ("pyyaml",),
    "sanka-connector-csv": (),
    "sanka-connector-sqlite": (),
    "sanka-connector-postgres": (
        "psycopg",
        "psycopg-binary",
        "typing-extensions",
        "tzdata",
    ),
    "sanka-connector-clickhouse": (
        "backports-zstd",
        "certifi",
        "clickhouse-connect",
        "lz4",
        "tzdata",
        "urllib3",
    ),
}
MANIFEST_WHEELS = {
    package: local
    + tuple(
        wheel.name
        for wheel in LOCKED_DEPENDENCY_WHEELS
        if wheel.distribution in MANIFEST_DEPENDENCIES[package]
    )
    for package, local in LOCAL_MANIFEST_WHEELS.items()
}
MANIFESTS = {package: ROOT / "packages" / package / "extension.json" for package in MANIFEST_WHEELS}


def _wheel_hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def update_manifests(release: Path, *, release_tag: str) -> dict[str, dict[str, Any]]:
    expected = {name for names in MANIFEST_WHEELS.values() for name in names}
    found = {path.name for path in release.glob("*.whl")}
    if release_tag != RELEASE_TAG or found != expected:
        raise RuntimeError(
            f"expected complete marketplace wheel set {sorted(expected)} for {RELEASE_TAG}; "
            f"found {sorted(found)} for {release_tag}"
        )
    return {
        package: {
            "wheels": [
                {
                    "name": name,
                    "url": (
                        "https://github.com/sankaHQ/extensions/releases/download/"
                        f"{release_tag}/{name}"
                    ),
                    "sha256": _wheel_hash(release / name),
                }
                for name in names
            ]
        }
        for package, names in MANIFEST_WHEELS.items()
    }


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--release-tag", required=True)
    args = parser.parse_args()
    for package, update in update_manifests(args.dist, release_tag=args.release_tag).items():
        manifest = json.loads(MANIFESTS[package].read_text(encoding="utf-8"))
        manifest.update(update)
        _atomic_write(MANIFESTS[package], manifest)
    print(f"Updated {len(MANIFESTS)} marketplace manifests for {args.release_tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

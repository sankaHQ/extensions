# SPDX-License-Identifier: Apache-2.0
"""Build and validate the separately published, immutable Jev converter closure."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts.build_release import PINNED_LOCAL_WHEELS, download_locked_wheel  # noqa: E402
from scripts.check_release_artifacts import _entry_points, _wheel_metadata  # noqa: E402

PACKAGE = "sanka-extension-llm-to-jev"
VERSION = "0.1.0a1"
TAG = f"llm-to-jev-v{VERSION}"
PREFIX = f"https://github.com/sankaHQ/extensions/releases/download/{TAG}/"
WHEEL = f"sanka_extension_llm_to_jev-{VERSION}-py3-none-any.whl"
MANIFEST_NAME = f"{PACKAGE}.json"
SDK_WHEELS = tuple(
    wheel
    for wheel in PINNED_LOCAL_WHEELS
    if wheel.distribution in {"sanka-extension-sdk", "sanka-connector-sdk"}
)
CATALOG = {
    "schema_version": "sanka-marketplace/v1",
    "extensions": [{"id": "sanka/llm-to-jev", "manifest": MANIFEST_NAME}],
}


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def manifest(root: Path = ROOT) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(
        (root / "packages" / PACKAGE / "extension.json").read_text()
    )
    template = json.loads((root / "packages" / PACKAGE / "extension.template.json").read_text())
    if result | {"wheels": []} != template:
        raise ValueError("Jev manifest differs from the reviewed converter contract")
    expected = [WHEEL, *(wheel.name for wheel in SDK_WHEELS)]
    entries = result.get("wheels")
    if not isinstance(entries, list) or [item.get("name") for item in entries] != expected:
        raise ValueError("Jev manifest must contain the exact three-wheel closure")
    pinned = {wheel.name: wheel.sha256 for wheel in SDK_WHEELS}
    for item in entries:
        sha = item.get("sha256", "")
        if (
            set(item) != {"name", "url", "sha256"}
            or item["url"] != PREFIX + item["name"]
            or len(sha) != 64
            or any(char not in "0123456789abcdef" for char in sha)
            or (item["name"] in pinned and sha != pinned[item["name"]])
        ):
            raise ValueError("Jev manifest has an invalid immutable URL or pinned wheel hash")
    return result


def validate(output: Path, root: Path = ROOT) -> None:
    expected = manifest(root)
    names = {item["name"] for item in expected["wheels"]} | {
        MANIFEST_NAME,
        "marketplace.json",
    }
    if {path.name for path in output.iterdir()} != names:
        raise ValueError("Jev release has missing or unexpected assets")
    for name in names:
        if (output / name).is_symlink() or not (output / name).is_file():
            raise ValueError("Jev release assets must be regular files")
    if json.loads((output / MANIFEST_NAME).read_text()) != expected:
        raise ValueError("Jev release manifest differs from reviewed source")
    if json.loads((output / "marketplace.json").read_text()) != CATALOG:
        raise ValueError("Jev release catalog differs from the scoped catalog")
    for item in expected["wheels"]:
        if digest(output / item["name"]) != item["sha256"]:
            raise ValueError(f"Jev release wheel hash mismatch: {item['name']}")
    metadata, entries, members = _wheel_metadata(output / WHEEL)
    requirements = [item.replace(" ", "") for item in metadata.get_all("Requires-Dist", [])]
    if (
        metadata["Name"] != PACKAGE
        or metadata["Version"] != VERSION
        or requirements != ["sanka-extension-sdk==0.1.0a4"]
        or _entry_points(entries, "console_scripts")
        != {PACKAGE: "sanka_extension_llm_to_jev.__main__:main"}
    ):
        raise ValueError("Jev wheel metadata/dependencies/entry point changed")
    required = {
        "sanka_extension_llm_to_jev/schemas/decision-v1.json",
        "sanka_extension_llm_to_jev/templates/adapter.py.tmpl",
        "sanka_extension_llm_to_jev/templates/compatibility.py.tmpl",
    }
    if not required <= members:
        raise ValueError("Jev wheel is missing generated-application templates or schema")


def build(output: Path) -> None:
    # Restrict cleanup/writes to a dedicated repository-owned release directory.
    absolute = output.absolute()
    if any(path.is_symlink() for path in (absolute, *absolute.parents)):
        raise ValueError("Jev release output must not traverse symlinks")
    output = absolute.resolve()
    if not output.is_relative_to(ROOT / "release") or output == ROOT / "release":
        raise ValueError("Jev release output must be below repository release/")
    expected = manifest()
    output.mkdir(parents=True, exist_ok=True)
    names = {item["name"] for item in expected["wheels"]} | {MANIFEST_NAME, "marketplace.json"}
    if any(path.name not in names or path.is_symlink() for path in output.iterdir()):
        raise ValueError("Jev output contains unexpected files or symlinks")
    subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--package",
            PACKAGE,
            "--out-dir",
            str(output),
            "--no-create-gitignore",
        ],
        cwd=ROOT,
        # Match Hatchling's deterministic default used by the accepted candidate.
        env=os.environ | {"SOURCE_DATE_EPOCH": "1580601600"},
        check=True,
    )
    for wheel in SDK_WHEELS:
        download_locked_wheel(output, wheel)
    (output / MANIFEST_NAME).write_text(json.dumps(expected, indent=2) + "\n")
    (output / "marketplace.json").write_text(json.dumps(CATALOG, indent=2) + "\n")
    validate(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "release" / "jev")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    if args.check_only:
        validate(args.output_dir)
    else:
        build(args.output_dir)
    print(f"Jev release verified: {TAG}; three immutable wheels and scoped catalog")


if __name__ == "__main__":
    main()

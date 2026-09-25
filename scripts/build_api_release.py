# SPDX-License-Identifier: Apache-2.0
"""Build the scoped, reproducible Go and Rust experimental release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts.build_release import PINNED_LOCAL_WHEELS, download_locked_wheel  # noqa: E402
from scripts.check_release_artifacts import _entry_points, _wheel_metadata  # noqa: E402

VERSION = "0.1.0a7"
TAG = f"api-converters-v{VERSION}"
PREFIX = f"https://github.com/sankaHQ/extensions/releases/download/{TAG}/"
RUST_PREFIX = "https://github.com/sankaHQ/extensions/releases/download/api-converters-v0.1.0a2/"
PACKAGES = (
    "sanka-extension-python-to-golang",
    "sanka-extension-typescript-to-rust",
    "sanka-ts-capture",
    "sanka-http-replay",
)
VERSIONS = dict.fromkeys(PACKAGES, "0.1.0a2")
VERSIONS[PACKAGES[0]] = VERSION
VERSIONS["sanka-ts-capture"] = "0.1.0a1"
SDK = tuple(
    w
    for w in PINNED_LOCAL_WHEELS
    if w.distribution in {"sanka-extension-sdk", "sanka-connector-sdk"}
)
DEPENDENCIES = {
    PACKAGES[0]: ["sanka-extension-sdk==0.1.0a4", "sanka-http-replay==0.1.0a2"],
    PACKAGES[1]: [
        "sanka-extension-sdk==0.1.0a4",
        "sanka-http-replay==0.1.0a2",
        "sanka-ts-capture==0.1.0a1",
    ],
    PACKAGES[2]: [],
    PACKAGES[3]: [],
}
TS_DIGEST = "3ae902c92cc44dace175c0e69e13a4b0899f6983c6121d76b9ab8dd5795e7675"
CATALOG = {
    "schema_version": "sanka-marketplace/v1",
    "extensions": [
        {"id": "sanka/" + p.removeprefix("sanka-extension-"), "manifest": f"{p}.json"}
        for p in PACKAGES[:2]
    ],
}


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def wheel_name(package: str) -> str:
    return f"{package.replace('-', '_')}-{VERSIONS[package]}-py3-none-any.whl"


def closure(package: str) -> list[str]:
    own = [package, *PACKAGES[2:]] if package == PACKAGES[1] else [package, PACKAGES[3]]
    return [*(wheel_name(p) for p in own), *(w.name for w in SDK)]


def manifest(package: str, root: Path = ROOT) -> dict[str, Any]:
    directory = root / "packages" / package
    result: dict[str, Any] = json.loads((directory / "extension.json").read_text())
    template = json.loads((directory / "extension.template.json").read_text())
    if result | {"wheels": []} != template:
        raise ValueError(f"{package}: manifest contract differs from template")
    entries = result.get("wheels", [])
    if [w.get("name") for w in entries] != closure(package):
        raise ValueError(f"{package}: incomplete wheel closure")
    sdk_hashes = {w.name: w.sha256 for w in SDK}
    prefix = PREFIX if package == PACKAGES[0] else RUST_PREFIX
    for wheel in entries:
        sha = wheel.get("sha256", "")
        if (
            set(wheel) != {"name", "url", "sha256"}
            or wheel["url"] != prefix + wheel["name"]
            or len(sha) != 64
            or any(c not in "0123456789abcdef" for c in sha)
            or (wheel["name"] in sdk_hashes and sha != sdk_hashes[wheel["name"]])
        ):
            raise ValueError(f"{package}: invalid URL or digest")
    return result


def validate(output: Path, root: Path = ROOT) -> None:
    manifests = {p: manifest(p, root) for p in PACKAGES[:2]}
    names = {wheel_name(p) for p in PACKAGES} | {w.name for w in SDK}
    assets = names | {f"{p}.json" for p in manifests} | {"marketplace.json"}
    if {p.name for p in output.iterdir()} != assets:
        raise ValueError("Missing or unexpected release assets")
    if any((output / n).is_symlink() or not (output / n).is_file() for n in assets):
        raise ValueError("Release assets must be regular files")
    if json.loads((output / "marketplace.json").read_text()) != CATALOG:
        raise ValueError("Scoped release catalog changed")
    for package, data in manifests.items():
        if json.loads((output / f"{package}.json").read_text()) != data:
            raise ValueError("Release manifest differs from reviewed source")
        for wheel in data["wheels"]:
            if digest(output / wheel["name"]) != wheel["sha256"]:
                raise ValueError(f"Wheel digest mismatch: {wheel['name']}")
    for package in PACKAGES:
        metadata, entries, members = _wheel_metadata(output / wheel_name(package))
        requirements = sorted(x.replace(" ", "") for x in metadata.get_all("Requires-Dist", []))
        expected_entry = {package: package.replace("-", "_") + ".__main__:main"}
        if (
            metadata["Name"] != package
            or metadata["Version"] != VERSIONS[package]
            or requirements != DEPENDENCIES[package]
            or _entry_points(entries, "console_scripts")
            != (expected_entry if package in PACKAGES[:2] else {})
        ):
            raise ValueError(f"Wheel metadata, dependencies or executable changed: {package}")
        module = package.replace("-", "_")
        if package == PACKAGES[0]:
            required = {
                f"{module}/locks/{target}/go.{suffix}"
                for target in ("fiber", "chi", "mux", "gin", "jwt")
                for suffix in ("mod", "sum")
            }
            if not required <= members:
                raise ValueError("Go wheel missing target locks")
        if package == PACKAGES[1] and not any(n.endswith("Cargo.lock") for n in members):
            raise ValueError("Rust wheel missing target lock")
        if package == PACKAGES[2]:
            with zipfile.ZipFile(output / wheel_name(package)) as archive:
                if (
                    hashlib.sha256(archive.read(f"{module}/node/typescript.js")).hexdigest()
                    != TS_DIGEST
                ):
                    raise ValueError("TypeScript compiler bundle digest changed")
                if f"{module}/node/LICENSE.typescript.txt" not in members:
                    raise ValueError("TypeScript license absent")


def build(output: Path, *, write_manifests: bool = False) -> None:
    absolute = output.absolute()
    if any(p.is_symlink() for p in (absolute, *absolute.parents)):
        raise ValueError("Release output must not traverse symlinks")
    output = absolute.resolve()
    if not output.is_relative_to(ROOT / "release") or output == ROOT / "release":
        raise ValueError("Output must be below repository release/")
    output.mkdir(parents=True, exist_ok=True)
    allowed = {wheel_name(p) for p in PACKAGES} | {w.name for w in SDK}
    allowed |= {f"{p}.json" for p in PACKAGES[:2]} | {"marketplace.json"}
    if any(p.name not in allowed or p.is_symlink() or not p.is_file() for p in output.iterdir()):
        raise ValueError("Release output contains unexpected files or symlinks")
    env = os.environ | {"SOURCE_DATE_EPOCH": "315532800"}
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/fetch_typescript_bundle.py")],
        cwd=ROOT,
        env=env,
        check=True,
    )
    for package in PACKAGES:
        subprocess.run(
            [
                "uv",
                "build",
                "--wheel",
                "--package",
                package,
                "--build-constraint",
                str(ROOT / "scripts/api-build-constraints.txt"),
                "--out-dir",
                str(output),
                "--no-create-gitignore",
            ],
            cwd=ROOT,
            env=env,
            check=True,
        )
    for wheel in SDK:
        download_locked_wheel(output, wheel)
    for package in PACKAGES[:2]:
        if write_manifests and package == PACKAGES[0]:
            directory = ROOT / "packages" / package
            data = json.loads((directory / "extension.template.json").read_text())
            data["wheels"] = [
                {"name": n, "url": PREFIX + n, "sha256": digest(output / n)}
                for n in closure(package)
            ]
            (directory / "extension.json").write_text(json.dumps(data, indent=2) + "\n")
        (output / f"{package}.json").write_text(json.dumps(manifest(package), indent=2) + "\n")
    (output / "marketplace.json").write_text(json.dumps(CATALOG, indent=2) + "\n")
    validate(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "release/api-converters")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--write-manifests", action="store_true")
    args = parser.parse_args()
    if args.check_only:
        validate(args.output_dir)
    else:
        build(args.output_dir, write_manifests=args.write_manifests)
    print(f"Validated {TAG}: six wheels, two manifests and scoped catalog")


if __name__ == "__main__":
    main()

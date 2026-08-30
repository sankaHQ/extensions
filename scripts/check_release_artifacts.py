# SPDX-License-Identifier: Apache-2.0
"""Validate wheel ownership, dependencies, and entry points before publishing."""

from __future__ import annotations

import email
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "release" / "all"


def _is_distribution(path: Path) -> bool:
    return path.suffix == ".whl" or path.name.endswith(".tar.gz")


def _wheel_metadata(wheel: Path) -> tuple[email.message.Message, str]:
    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = email.message_from_bytes(archive.read(metadata_name))
        entry_name = metadata_name.removesuffix("METADATA") + "entry_points.txt"
        entries = archive.read(entry_name).decode() if entry_name in archive.namelist() else ""
    return metadata, entries


def main() -> int:
    errors: list[str] = []
    expected_packages = sorted(path.name for path in (ROOT / "packages").iterdir() if path.is_dir())
    expected_count = len(expected_packages)
    unexpected = sorted(path.name for path in RELEASE.iterdir() if not _is_distribution(path))
    if unexpected:
        errors.append(f"combined release directory contains non-distributions: {unexpected}")

    package_root = RELEASE.parent / "packages"
    package_directories = sorted(path for path in package_root.iterdir() if path.is_dir())
    released_packages = [path.name for path in package_directories]
    if released_packages != expected_packages:
        errors.append(
            f"expected package release directories {expected_packages}, found {released_packages}"
        )
    for package_directory in package_directories:
        package_files = sorted(path for path in package_directory.iterdir() if path.is_file())
        package_unexpected = [path.name for path in package_files if not _is_distribution(path)]
        if package_unexpected:
            errors.append(
                f"{package_directory.name} publish directory contains non-distributions: "
                f"{package_unexpected}"
            )
        package_artifacts = [path for path in package_files if _is_distribution(path)]
        if len(package_artifacts) != 2:
            errors.append(
                f"{package_directory.name} publish directory expected 2 distributions, "
                f"found {len(package_artifacts)}"
            )

    wheels = sorted(RELEASE.glob("*.whl"))
    sdists = sorted(RELEASE.glob("*.tar.gz"))
    if len(wheels) != expected_count:
        errors.append(f"expected {expected_count} wheels, found {len(wheels)}")
    if len(sdists) != expected_count:
        errors.append(f"expected {expected_count} sdists, found {len(sdists)}")

    for wheel in wheels:
        metadata, entries = _wheel_metadata(wheel)
        name = str(metadata["Name"])
        requirements = metadata.get_all("Requires-Dist", [])
        if name == "sanka-connector-sdk":
            if requirements:
                errors.append(f"SDK wheel has runtime dependencies: {requirements}")
            if entries:
                errors.append("SDK wheel must not register a provider entry point")
        else:
            if not any(
                str(item).lower().startswith("sanka-connector-sdk") for item in requirements
            ):
                errors.append(f"{name} wheel does not depend on sanka-connector-sdk")
            if "[sanka.connectors]" not in entries:
                errors.append(f"{name} wheel has no sanka.connectors entry point")

    if errors:
        print("Release artifact validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Connector release artifacts: OK ({len(wheels)} wheels)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

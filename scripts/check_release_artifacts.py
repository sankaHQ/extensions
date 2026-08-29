# SPDX-License-Identifier: Apache-2.0
"""Validate wheel ownership, dependencies, and entry points before publishing."""

from __future__ import annotations

import email
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "release" / "all"


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
    wheels = sorted(RELEASE.glob("*.whl"))
    sdists = sorted(RELEASE.glob("*.tar.gz"))
    if len(wheels) != 9:
        errors.append(f"expected 9 wheels, found {len(wheels)}")
    if len(sdists) != 9:
        errors.append(f"expected 9 sdists, found {len(sdists)}")

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

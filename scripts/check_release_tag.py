# SPDX-License-Identifier: Apache-2.0
"""Require an exact version tag shared by every connector distribution."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: check_release_tag.py <ref-name> <ref-type>", file=sys.stderr)
        return 2
    ref_name, ref_type = argv[1:]
    versions: set[str] = set()
    for pyproject in sorted((ROOT / "packages").glob("*/pyproject.toml")):
        with pyproject.open("rb") as handle:
            versions.add(str(tomllib.load(handle)["project"]["version"]))
    if len(versions) != 1:
        print(f"connector package versions differ: {sorted(versions)}", file=sys.stderr)
        return 1
    version = versions.pop()
    expected = f"v{version}"
    if ref_type != "tag" or ref_name != expected:
        print(
            f"release must run from exact tag {expected}; got {ref_type} {ref_name}",
            file=sys.stderr,
        )
        return 1
    print(f"release tag matches all connector packages: {expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

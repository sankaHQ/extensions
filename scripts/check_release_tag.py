# SPDX-License-Identifier: Apache-2.0
"""Require the exact tag for one independently versioned release family."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def expected_tag(family: str, root: Path = ROOT) -> str:
    if family not in {"connectors", "extensions"}:
        raise ValueError(f"unknown release family: {family}")
    prefix = f"sanka-{family.removesuffix('s')}-"
    versions: set[str] = set()
    for pyproject in sorted((root / "packages").glob(f"{prefix}*/pyproject.toml")):
        with pyproject.open("rb") as handle:
            versions.add(str(tomllib.load(handle)["project"]["version"]))
    if len(versions) != 1:
        raise ValueError(f"{family} package versions differ: {sorted(versions)}")
    version = versions.pop()
    return f"v{version}" if family == "connectors" else f"extensions-v{version}"


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(
            "usage: check_release_tag.py <connectors|extensions> <ref-name> <ref-type>",
            file=sys.stderr,
        )
        return 2
    family, ref_name, ref_type = argv[1:]
    try:
        expected = expected_tag(family)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    if ref_type != "tag" or ref_name != expected:
        print(
            f"release must run from exact tag {expected}; got {ref_type} {ref_name}",
            file=sys.stderr,
        )
        return 1
    print(f"release tag matches all {family} packages: {expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

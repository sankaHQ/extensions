# SPDX-License-Identifier: Apache-2.0
"""Fetch the pinned parser once; installed wheels analyze without network access."""

from __future__ import annotations

import hashlib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "3.28.2"
SHA256 = "b5499a3b1c40b16c0671fabe478c9aafeab38160c6fde74a6c13f42d86716ecd"
DESTINATION = (
    ROOT / "packages/sanka-java-migration/src/sanka_java_migration/java/javaparser-core.jar"
)
URL = (
    "https://repo.maven.apache.org/maven2/com/github/javaparser/javaparser-core/"
    f"{VERSION}/javaparser-core-{VERSION}.jar"
)


def main() -> None:
    if DESTINATION.is_symlink():
        raise ValueError("parser destination must not be a symlink")
    if DESTINATION.is_file():
        if hashlib.sha256(DESTINATION.read_bytes()).hexdigest() != SHA256:
            raise ValueError("existing parser digest mismatch; remove it explicitly before retry")
        return
    with urllib.request.urlopen(URL, timeout=60) as response:
        data = response.read(2_000_001)
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError("JavaParser download digest mismatch")
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    DESTINATION.write_bytes(data)
    print(f"Verified JavaParser {VERSION}")


if __name__ == "__main__":
    main()

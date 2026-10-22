# SPDX-License-Identifier: Apache-2.0
"""Prepare an isolated, checksum-verified Maven distribution for qualification."""

from __future__ import annotations

import hashlib
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "3.9.16"
SHA256 = "80ffca22aed9e8b9713a232f3394fd81d7f20322df75efdb2b047dbd3e3a23bb"
TOOLS = ROOT / ".java-tools"
URL = (
    "https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/"
    f"{VERSION}/apache-maven-{VERSION}-bin.tar.gz"
)


def main() -> None:
    if TOOLS.is_symlink():
        raise ValueError("tool directory must not be a symlink")
    TOOLS.mkdir(exist_ok=True)
    archive = TOOLS / f"apache-maven-{VERSION}-bin.tar.gz"
    if archive.is_symlink():
        raise ValueError("Maven archive must not be a symlink")
    if not archive.exists():
        with urllib.request.urlopen(URL, timeout=60) as response:
            data = response.read(20_000_001)
        if hashlib.sha256(data).hexdigest() != SHA256:
            raise ValueError("Maven download digest mismatch")
        archive.write_bytes(data)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise ValueError("existing Maven archive digest mismatch")
    destination = TOOLS / f"apache-maven-{VERSION}"
    if destination.is_symlink():
        raise ValueError("Maven directory must not be a symlink")
    if not destination.exists():
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                path = (TOOLS / member.name).resolve()
                if not path.is_relative_to(destination) or not (member.isfile() or member.isdir()):
                    raise ValueError("unexpected Maven archive entry")
            tar.extractall(TOOLS, filter="data")
    (TOOLS / "maven-repository").mkdir(exist_ok=True)
    print(destination / "bin/mvn")


if __name__ == "__main__":
    main()

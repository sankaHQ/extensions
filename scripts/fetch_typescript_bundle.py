# SPDX-License-Identifier: Apache-2.0
"""Place the pinned TypeScript compiler bundle into sanka-ts-capture.

The 9 MB ``lib/typescript.js`` bundle is not committed. This script downloads the
exact npm tarball, verifies the tarball and bundle digests, and writes the bundle
where the package (and the built wheel) expect it. It is idempotent: an existing
bundle with the pinned digest is kept without network access.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import tarfile
import urllib.request
from pathlib import Path

from sanka_ts_capture.driver import NODE_ROOT, TYPESCRIPT_SHA256, TYPESCRIPT_VERSION

ROOT = Path(__file__).resolve().parents[1]
TARBALL_URL = f"https://registry.npmjs.org/typescript/-/typescript-{TYPESCRIPT_VERSION}.tgz"
TARBALL_SHA256 = "10e108c9cf7d5f2879053dff18515fb405abf2ccef63eaaf017d9c571687a1d3"
MEMBER = "package/lib/typescript.js"
MAX_BYTES = 64 * 1024 * 1024
DESTINATION = NODE_ROOT / "typescript.js"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tarball(path: Path | None) -> bytes:
    if path is not None:
        data = path.read_bytes()
    else:
        with urllib.request.urlopen(TARBALL_URL, timeout=120) as response:
            data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise SystemExit("typescript tarball exceeds the expected size")
    if _sha256(data) != TARBALL_SHA256:
        raise SystemExit("typescript tarball digest does not match the pinned release")
    return data


def _bundle(data: bytes) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        try:
            member = archive.getmember(MEMBER)
        except KeyError as error:
            raise SystemExit(f"typescript tarball has no {MEMBER}") from error
        if not member.isfile() or member.size > MAX_BYTES:
            raise SystemExit(f"{MEMBER} is not a regular bounded file")
        stream = archive.extractfile(member)
        bundle = stream.read() if stream is not None else b""
    if _sha256(bundle) != TYPESCRIPT_SHA256:
        raise SystemExit("typescript bundle digest does not match the pinned release")
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tarball",
        type=Path,
        help="Use an already downloaded typescript tarball instead of fetching it.",
    )
    parser.add_argument("--force", action="store_true", help="Replace an existing bundle.")
    args = parser.parse_args()
    relative = DESTINATION.relative_to(ROOT) if DESTINATION.is_relative_to(ROOT) else DESTINATION
    if (
        not args.force
        and DESTINATION.is_file()
        and _sha256(DESTINATION.read_bytes()) == TYPESCRIPT_SHA256
    ):
        print(f"TypeScript {TYPESCRIPT_VERSION} bundle already present: {relative}")
        return 0
    bundle = _bundle(_tarball(args.tarball))
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    temporary = DESTINATION.with_name("typescript.js.part")
    temporary.write_bytes(bundle)
    temporary.replace(DESTINATION)
    print(f"TypeScript {TYPESCRIPT_VERSION} bundle written: {relative} ({len(bundle)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

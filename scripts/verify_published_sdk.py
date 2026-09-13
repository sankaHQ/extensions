# SPDX-License-Identifier: Apache-2.0
"""Verify an unchanged SDK against its existing immutable public release."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts import ensure_sdk_release_tag  # noqa: E402

SDK_REVISION = "b52bf22f60b2a3704bf0414d609c3e3f767bcd41"
SDK_WHEELS = {
    "sanka_connector_sdk-0.1.0a12-py3-none-any.whl": (
        "34da5c35aaa60fc19258e76b72a3eca58bf52fff96e2ccf9a0aa1115f8878d8e"
    ),
    "sanka_extension_sdk-0.1.0a4-py3-none-any.whl": (
        "f1a6655095ab81e549137e1d9604492bda2a31677d674c6359b0a39a2da96307"
    ),
}
MAX_WHEEL_BYTES = 16 * 1024 * 1024


def verify_published_sdk(directory: Path) -> None:
    # No tag creation or upload: this release consumes the already published SDK.
    if ensure_sdk_release_tag.remote_revision() != SDK_REVISION:
        raise RuntimeError("Published SDK tag no longer matches its reviewed source")
    for name, expected in SDK_WHEELS.items():
        with (directory / name).open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != expected:
                raise RuntimeError(f"Changed SDK wheel needs a new SDK version: {name}")
        url = (
            "https://github.com/sankaHQ/extensions/releases/download/"
            f"{ensure_sdk_release_tag.TAG}/{name}"
        )
        with urlopen(url, timeout=30) as response:
            published = response.read(MAX_WHEEL_BYTES + 1)
        if len(published) > MAX_WHEEL_BYTES or hashlib.sha256(published).hexdigest() != expected:
            raise RuntimeError(f"Published SDK asset does not match reviewed bytes: {name}")
    print(f"Verified {len(SDK_WHEELS)} unchanged SDK wheels at {ensure_sdk_release_tag.TAG}")


if __name__ == "__main__":
    verify_published_sdk(Path(sys.argv[1]))

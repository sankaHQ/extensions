# SPDX-License-Identifier: Apache-2.0
"""Record the exact official extension wheels in the marketplace manifest."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "packages" / "sanka-extension-drf-to-fastapi" / "extension.json"
RELEASE_TAG = "extensions-v0.1.0a1"
WHEEL_NAMES = (
    "sanka_extension_sdk-0.1.0a1-py3-none-any.whl",
    "sanka_extension_drf_to_fastapi-0.1.0a1-py3-none-any.whl",
)


def update_manifest(release: Path, *, release_tag: str) -> dict[str, Any]:
    found = {path.name for path in release.glob("sanka_extension_*.whl")}
    if release_tag != RELEASE_TAG or found != set(WHEEL_NAMES):
        raise RuntimeError(
            f"expected complete extension wheel set {list(WHEEL_NAMES)} for {RELEASE_TAG}; "
            f"found {sorted(found)} for {release_tag}"
        )
    wheels: list[dict[str, str]] = []
    for name in WHEEL_NAMES:
        path = release / name
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        wheels.append(
            {
                "name": name,
                "url": f"https://github.com/sankaHQ/extensions/releases/download/{release_tag}/{name}",
                "sha256": digest,
            }
        )
    return {"wheels": wheels}


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(
            "usage: update_marketplace_hashes.py <release-directory> <release-tag>",
            file=sys.stderr,
        )
        return 2
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest.update(update_manifest(Path(argv[1]), release_tag=argv[2]))
    _atomic_write(MANIFEST, manifest)
    print(f"Updated {MANIFEST.relative_to(ROOT)} for {argv[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

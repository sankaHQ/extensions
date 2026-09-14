# SPDX-License-Identifier: Apache-2.0
"""Validate the separately versioned SDK candidate without changing marketplace pins."""

from __future__ import annotations

import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts.check_release_artifacts import _wheel_metadata  # noqa: E402


def validate(directory: Path, *, root: Path = ROOT) -> None:
    project = tomllib.loads((root / "packages/sanka-extension-sdk/pyproject.toml").read_text())[
        "project"
    ]
    version = project["version"]
    if version in {"0.1.0a4", "0.1.0a5"}:
        raise ValueError("Candidate SDK must not reuse a published SDK version")
    expected = directory / f"sanka_extension_sdk-{version}-py3-none-any.whl"
    if sorted(directory.iterdir()) != [expected]:
        raise ValueError("SDK candidate directory must contain exactly its versioned wheel")
    metadata, entries = _wheel_metadata(expected)
    if (metadata["Name"], metadata["Version"]) != ("sanka-extension-sdk", version):
        raise ValueError("SDK candidate metadata does not match its source")
    if metadata["License-Expression"] != "Apache-2.0" or entries:
        raise ValueError("SDK candidate must remain Apache-2.0 without executable entry points")
    requirements = [v.replace(" ", "") for v in metadata.get_all("Requires-Dist", [])]
    if requirements != ["sanka-connector-sdk==0.1.0a12"]:
        raise ValueError("SDK candidate has unexpected dependencies")
    with zipfile.ZipFile(expected) as archive:
        if "sanka_extensions/flow/native.py" not in archive.namelist():
            raise ValueError("SDK candidate omits the native contract")
        for module in ("native_verification", "native_fixtures"):
            if f"sanka_extensions/flow/{module}.py" not in archive.namelist():
                raise ValueError("SDK candidate omits the native verification contract")
    print(f"SDK candidate {version}: metadata, license, dependencies and native contract OK")


if __name__ == "__main__":
    validate(Path(sys.argv[1]))

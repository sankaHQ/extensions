# SPDX-License-Identifier: Apache-2.0
"""Build a separate Business Flow candidate with immutable published SDK dependencies."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts.build_release import LockedWheel, download_locked_wheel  # noqa: E402
from scripts.check_release_artifacts import _entry_points, _wheel_metadata  # noqa: E402

PACKAGE = "sanka-extension-business-flows"
VERSION = "0.1.0a1"
TAG = f"business-flows-v{VERSION}"
WHEEL = f"sanka_extension_business_flows-{VERSION}-py3-none-any.whl"
SDK_WHEELS = (
    LockedWheel(
        "sanka-extension-sdk",
        "sanka_extension_sdk-0.1.0a5-py3-none-any.whl",
        "https://github.com/sankaHQ/extensions/releases/download/sdk-v0.1.0a5/"
        "sanka_extension_sdk-0.1.0a5-py3-none-any.whl",
        "259d80145d4cf75875cd6aa38091da3688d80b469ac28968b531f53bda8f41ab",
        49163,
    ),
    LockedWheel(
        "sanka-connector-sdk",
        "sanka_connector_sdk-0.1.0a12-py3-none-any.whl",
        "https://github.com/sankaHQ/extensions/releases/download/sdk-v0.1.0a5/"
        "sanka_connector_sdk-0.1.0a12-py3-none-any.whl",
        "34da5c35aaa60fc19258e76b72a3eca58bf52fff96e2ccf9a0aa1115f8878d8e",
        17355,
    ),
)
CATALOG = {
    "schema_version": "sanka-marketplace/v1",
    "extensions": [{"id": "sanka/business-flows", "manifest": "extension.json"}],
}


def manifest(sha256: str) -> dict[str, Any]:
    from sanka_extension_business_flows import capability

    return {
        "schema_version": "sanka-extension-manifest/v2",
        "id": "sanka/business-flows",
        "version": VERSION,
        "kind": "flow",
        "protocol_version": "sanka-flow-extension/v1",
        "commands": ["blueprint"],
        "capabilities": [capability().to_dict()],
        "runtime": {"sanka_cli": ">=0.2.13,<0.3"},
        "distribution": {"name": PACKAGE, "version": VERSION, "executable": PACKAGE},
        "wheels": [
            *({"name": w.name, "url": w.url, "sha256": w.sha256} for w in SDK_WHEELS),
            {
                "name": WHEEL,
                "url": f"https://github.com/sankaHQ/extensions/releases/download/{TAG}/{WHEEL}",
                "sha256": sha256,
            },
        ],
    }


def build(*, update_manifest: bool = False) -> None:
    directory = ROOT / "release/business-flows"
    directory.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--package",
            PACKAGE,
            "--out-dir",
            str(directory),
            "--no-create-gitignore",
        ],
        cwd=ROOT,
        env=os.environ | {"SOURCE_DATE_EPOCH": "315532800"},
        check=True,
    )
    wheel = directory / WHEEL
    metadata, entries = _wheel_metadata(wheel)
    project = tomllib.loads((ROOT / "packages" / PACKAGE / "pyproject.toml").read_text())["project"]
    if (metadata["Name"], metadata["Version"]) != (PACKAGE, VERSION) or project[
        "version"
    ] != VERSION:
        raise ValueError("Business Flow distribution/version differs from the candidate")
    requirements = [v.replace(" ", "") for v in metadata.get_all("Requires-Dist", [])]
    if metadata["License-Expression"] != "Apache-2.0" or requirements != [
        "sanka-extension-sdk==0.1.0a5"
    ]:
        raise ValueError("Business Flow wheel changed its license or SDK-only dependency")
    if _entry_points(entries, "console_scripts") != {
        PACKAGE: "sanka_extension_business_flows.__main__:main"
    }:
        raise ValueError("Business Flow wheel must expose its exact isolated executable")
    with zipfile.ZipFile(wheel) as archive:
        packaged = archive.read("sanka_extension_business_flows/catalog.json")
        source = (
            ROOT / "packages" / PACKAGE / "src/sanka_extension_business_flows/catalog.json"
        ).read_bytes()
        if packaged != source:
            raise ValueError("Business Flow wheel omitted or changed its canonical catalog")
    for sdk in SDK_WHEELS:
        download_locked_wheel(directory, sdk)
    expected = manifest(hashlib.sha256(wheel.read_bytes()).hexdigest())
    snapshot = ROOT / "business-flows"
    if update_manifest:
        snapshot.mkdir(exist_ok=True)
        (snapshot / "extension.json").write_text(json.dumps(expected, indent=2) + "\n")
        (snapshot / "marketplace.json").write_text(json.dumps(CATALOG, indent=2) + "\n")
    if json.loads((snapshot / "extension.json").read_text()) != expected:
        raise ValueError("Business Flow manifest drift: review and update candidate hashes")
    if json.loads((snapshot / "marketplace.json").read_text()) != CATALOG:
        raise ValueError("Business Flow marketplace differs from the candidate")
    for name in ("extension.json", "marketplace.json"):
        shutil.copyfile(snapshot / name, directory / name)
    if {p.name for p in directory.iterdir()} != {
        WHEEL,
        *(w.name for w in SDK_WHEELS),
        "extension.json",
        "marketplace.json",
    }:
        raise ValueError("Business Flow output contains unexpected release artifacts")
    print("Business Flow candidate: wheel, catalog, manifest and published SDK hashes verified")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update-manifest", action="store_true")
    build(update_manifest=parser.parse_args().update_manifest)

# SPDX-License-Identifier: Apache-2.0
"""Public marketplace contract for runtime extension discovery."""

import hashlib
import json
import shutil
from pathlib import Path

from scripts.check_release_artifacts import ROOT, _catalog_errors

RELEASE_PREFIX = "https://github.com/sankaHQ/extensions/releases/download/"
EXPECTED = {
    "sanka/react-native-to-native": {
        "kind": "migration",
        "protocol_version": "sanka-extension/v1",
        "distribution": {
            "name": "sanka-extension-react-native-to-native",
            "version": "0.1.0a1",
            "executable": "sanka-extension-react-native-to-native",
        },
    },
    "sanka/python-to-golang": {
        "kind": "migration",
        "protocol_version": "sanka-extension/v1",
        "distribution": {
            "name": "sanka-extension-python-to-golang",
            "version": "0.1.0a14",
            "executable": "sanka-extension-python-to-golang",
        },
    },
    "sanka/typescript-to-rust": {
        "kind": "migration",
        "protocol_version": "sanka-extension/v1",
        "distribution": {
            "name": "sanka-extension-typescript-to-rust",
            "version": "0.1.0a3",
            "executable": "sanka-extension-typescript-to-rust",
        },
    },
    "sanka/llm-to-jev": {
        "kind": "migration",
        "protocol_version": "sanka-extension/v1",
        "distribution": {
            "name": "sanka-extension-llm-to-jev",
            "version": "0.1.0a1",
            "executable": "sanka-extension-llm-to-jev",
        },
    },
    "sanka/drf-to-flask": {
        "kind": "migration",
        "protocol_version": "sanka-extension/v1",
        "distribution": {
            "name": "sanka-extension-drf-to-flask",
            "version": "0.1.0a15",
            "executable": "sanka-extension-drf-to-flask",
        },
    },
    "sanka/drf-to-fastapi": {
        "kind": "migration",
        "protocol_version": "sanka-extension/v1",
        "distribution": {
            "name": "sanka-extension-drf-to-fastapi",
            "version": "0.1.0a21",
            "executable": "sanka-extension-drf-to-fastapi",
        },
    },
}


def test_official_marketplace_has_only_current_code_extensions() -> None:
    catalog = json.loads(Path("marketplace.json").read_text())

    assert catalog["schema_version"] == "sanka-marketplace/v1"
    assert {item["id"] for item in catalog["extensions"]} == set(EXPECTED)
    for item in catalog["extensions"]:
        manifest = json.loads(Path(item["manifest"]).read_text())
        expected = EXPECTED[item["id"]]
        assert manifest["schema_version"] == "sanka-extension-manifest/v2"
        assert manifest["id"] == item["id"]
        cli = "==0.2.12" if item["id"] == "sanka/llm-to-jev" else ">=0.2.0,<0.4"
        if item["id"] in {
            "sanka/react-native-to-native",
            "sanka/python-to-golang",
            "sanka/typescript-to-rust",
        }:
            cli = ">=0.2.12,<0.4"
        assert manifest["runtime"] == {"sanka_cli": cli}
        assert manifest["kind"] == expected["kind"]
        assert manifest["protocol_version"] == expected["protocol_version"]
        assert manifest["distribution"] == expected["distribution"]
        assert manifest["wheels"]
        expected_prefix = RELEASE_PREFIX
        if item["id"] == "sanka/drf-to-flask":
            expected_prefix = RELEASE_PREFIX + "extensions-v0.1.0a36/"
        if item["id"] == "sanka/drf-to-fastapi":
            expected_prefix = RELEASE_PREFIX + "extensions-v0.1.0a36/"
        if item["id"] == "sanka/llm-to-jev":
            expected_prefix = RELEASE_PREFIX + "llm-to-jev-v0.1.0a1/"
        if item["id"] == "sanka/python-to-golang":
            expected_prefix = RELEASE_PREFIX + "api-converters-v0.1.0a14/"
        if item["id"] == "sanka/typescript-to-rust":
            expected_prefix = RELEASE_PREFIX + "api-converters-v0.1.0a14/"
        if item["id"] == "sanka/react-native-to-native":
            expected_prefix = RELEASE_PREFIX + "mobile-converters-v0.1.0a1/"
        assert all(wheel["url"].startswith(expected_prefix) for wheel in manifest["wheels"])
        assert all(len(wheel["sha256"]) == 64 for wheel in manifest["wheels"])


def test_candidate_hashes_do_not_change_publication_or_dependency_validation(
    tmp_path: Path,
) -> None:
    snapshot, bundle = tmp_path / "snapshot", tmp_path / "bundle"
    snapshot.mkdir()
    bundle.mkdir()
    shutil.copyfile(ROOT / "marketplace.json", snapshot / "marketplace.json")
    for path in (ROOT / "packages").glob("*/extension*.json"):
        destination = snapshot / path.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    for package in ("sanka-extension-drf-to-fastapi", "sanka-extension-drf-to-flask"):
        path = snapshot / "packages" / package / "extension.json"
        manifest = json.loads(path.read_text())
        for wheel in manifest["wheels"]:
            artifact = bundle / wheel["name"]
            artifact.write_bytes(b"reviewed bytes")
            wheel["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
        path.write_text(json.dumps(manifest))
    assert not _catalog_errors(snapshot, bundle)
    for artifact in bundle.glob("*.whl"):
        if artifact.name.startswith(("sanka_extension_drf_to_flask-", "sanka_drf_replay-")):
            artifact.write_bytes(b"changed source candidate")
    assert _catalog_errors(snapshot, bundle)
    assert not _catalog_errors(snapshot, bundle, candidate=True)
    sdk = next(bundle.glob("sanka_extension_sdk-*.whl"))
    sdk.write_bytes(b"changed published dependency")
    assert _catalog_errors(snapshot, bundle, candidate=True)

# SPDX-License-Identifier: Apache-2.0
"""Public marketplace contract for runtime extension discovery."""

import json
from pathlib import Path

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
            "version": "0.1.0a8",
            "executable": "sanka-extension-python-to-golang",
        },
    },
    "sanka/typescript-to-rust": {
        "kind": "migration",
        "protocol_version": "sanka-extension/v1",
        "distribution": {
            "name": "sanka-extension-typescript-to-rust",
            "version": "0.1.0a2",
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
            "version": "0.1.0a12",
            "executable": "sanka-extension-drf-to-flask",
        },
    },
    "sanka/drf-to-fastapi": {
        "kind": "migration",
        "protocol_version": "sanka-extension/v1",
        "distribution": {
            "name": "sanka-extension-drf-to-fastapi",
            "version": "0.1.0a18",
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
            expected_prefix = RELEASE_PREFIX + "extensions-v0.1.0a31/"
        if item["id"] == "sanka/drf-to-fastapi":
            expected_prefix = RELEASE_PREFIX + "extensions-v0.1.0a32/"
        if item["id"] == "sanka/llm-to-jev":
            expected_prefix = RELEASE_PREFIX + "llm-to-jev-v0.1.0a1/"
        if item["id"] == "sanka/python-to-golang":
            expected_prefix = RELEASE_PREFIX + "api-converters-v0.1.0a8/"
        if item["id"] == "sanka/typescript-to-rust":
            expected_prefix = RELEASE_PREFIX + "api-converters-v0.1.0a2/"
        if item["id"] == "sanka/react-native-to-native":
            expected_prefix = RELEASE_PREFIX + "mobile-converters-v0.1.0a1/"
        assert all(wheel["url"].startswith(expected_prefix) for wheel in manifest["wheels"])
        assert all(len(wheel["sha256"]) == 64 for wheel in manifest["wheels"])

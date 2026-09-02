# SPDX-License-Identifier: Apache-2.0
"""Public marketplace contract for runtime extension discovery."""

import json
from pathlib import Path

RELEASE_PREFIX = "https://github.com/sankaHQ/extensions/releases/download/extensions-v0.1.0a11/"
EXPECTED = {
    "sanka/drf-to-fastapi": {
        "kind": "migration",
        "protocol_version": "sanka-extension/v1",
        "distribution": {
            "name": "sanka-extension-drf-to-fastapi",
            "version": "0.1.0a1",
            "executable": "sanka-extension-drf-to-fastapi",
        },
    },
    "sanka/markdown": {
        "kind": "connector",
        "protocol_version": "sanka-connector/v1",
        "distribution": {
            "name": "sanka-connector-markdown",
            "version": "0.1.0a11",
            "entry_point": "markdown",
        },
        "providers": [{"name": "markdown", "roles": ["source"]}],
    },
    "sanka/csv": {
        "kind": "connector",
        "protocol_version": "sanka-connector/v1",
        "distribution": {
            "name": "sanka-connector-csv",
            "version": "0.1.0a11",
            "entry_point": "csv",
        },
        "providers": [{"name": "csv", "roles": ["source"]}],
    },
    "sanka/sqlite": {
        "kind": "connector",
        "protocol_version": "sanka-connector/v1",
        "distribution": {
            "name": "sanka-connector-sqlite",
            "version": "0.1.0a11",
            "entry_point": "sqlite",
        },
        "providers": [{"name": "sqlite", "roles": ["source", "destination"]}],
    },
    "sanka/postgres": {
        "kind": "connector",
        "protocol_version": "sanka-connector/v1",
        "distribution": {
            "name": "sanka-connector-postgres",
            "version": "0.1.0a11",
            "entry_point": "postgres",
        },
        "providers": [{"name": "postgres", "roles": ["source", "destination"]}],
    },
    "sanka/clickhouse": {
        "kind": "connector",
        "protocol_version": "sanka-connector/v1",
        "distribution": {
            "name": "sanka-connector-clickhouse",
            "version": "0.1.0a11",
            "entry_point": "clickhouse",
        },
        "providers": [{"name": "clickhouse", "roles": ["destination"]}],
    },
}


def test_official_marketplace_has_migration_and_connector_components() -> None:
    catalog = json.loads(Path("marketplace.json").read_text())

    assert catalog["schema_version"] == "sanka-marketplace/v1"
    assert {item["id"] for item in catalog["extensions"]} == set(EXPECTED)
    for item in catalog["extensions"]:
        manifest = json.loads(Path(item["manifest"]).read_text())
        expected = EXPECTED[item["id"]]
        assert manifest["schema_version"] == "sanka-extension-manifest/v2"
        assert manifest["id"] == item["id"]
        assert manifest["runtime"] == {"sanka_cli": ">=0.2.0,<0.3"}
        assert manifest["kind"] == expected["kind"]
        assert manifest["protocol_version"] == expected["protocol_version"]
        assert manifest["distribution"] == expected["distribution"]
        if "providers" in expected:
            assert manifest["providers"] == expected["providers"]
        assert manifest["wheels"]
        assert all(wheel["url"].startswith(RELEASE_PREFIX) for wheel in manifest["wheels"])
        assert all(len(wheel["sha256"]) == 64 for wheel in manifest["wheels"])

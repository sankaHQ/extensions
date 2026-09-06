# SPDX-License-Identifier: Apache-2.0
"""Public marketplace contract for runtime extension discovery."""

import json
from pathlib import Path

import yaml

RELEASE_PREFIX = "https://github.com/sankaHQ/extensions/releases/download/extensions-v0.1.0a14/"
EXPECTED = {
    "sanka/drf-to-flask": {
        "kind": "migration",
        "protocol_version": "sanka-extension/v1",
        "distribution": {
            "name": "sanka-extension-drf-to-flask",
            "version": "0.1.0a1",
            "executable": "sanka-extension-drf-to-flask",
        },
    },
    "sanka/drf-to-fastapi": {
        "kind": "migration",
        "protocol_version": "sanka-extension/v1",
        "distribution": {
            "name": "sanka-extension-drf-to-fastapi",
            "version": "0.1.0a3",
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


def test_release_workflow_stages_each_manifest_under_a_unique_asset_name() -> None:
    workflow = yaml.safe_load(Path(".github/workflows/publish.yml").read_text())
    release_steps = workflow["jobs"]["release"]["steps"]
    staging = next(step["run"] for step in release_steps if "release-assets" in step.get("run", ""))

    destinations = [
        line.rsplit(" ", 1)[-1]
        for line in staging.splitlines()
        if line.startswith("cp packages/") and line.endswith(".json")
    ]
    assert destinations == [
        "release-assets/sanka-extension-drf-to-fastapi.json",
        "release-assets/sanka-extension-drf-to-flask.json",
        "release-assets/sanka-connector-markdown.json",
        "release-assets/sanka-connector-csv.json",
        "release-assets/sanka-connector-sqlite.json",
        "release-assets/sanka-connector-postgres.json",
        "release-assets/sanka-connector-clickhouse.json",
    ]
    assert len(destinations) == len(set(destinations))

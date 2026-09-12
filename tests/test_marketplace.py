# SPDX-License-Identifier: Apache-2.0
"""Public marketplace contract for runtime extension discovery."""

import json
from pathlib import Path

import yaml

RELEASE_PREFIX = "https://github.com/sankaHQ/extensions/releases/download/extensions-v0.1.0a18/"
EXPECTED = {
    "sanka/drf-to-flask": {
        "kind": "migration",
        "protocol_version": "sanka-extension/v1",
        "distribution": {
            "name": "sanka-extension-drf-to-flask",
            "version": "0.1.0a4",
            "executable": "sanka-extension-drf-to-flask",
        },
    },
    "sanka/drf-to-fastapi": {
        "kind": "migration",
        "protocol_version": "sanka-extension/v1",
        "distribution": {
            "name": "sanka-extension-drf-to-fastapi",
            "version": "0.1.0a6",
            "executable": "sanka-extension-drf-to-fastapi",
        },
    },
    "sanka/markdown": {
        "kind": "connector",
        "protocol_version": "sanka-connector/v1",
        "distribution": {
            "name": "sanka-connector-markdown",
            "version": "0.1.0a13",
            "entry_point": "markdown",
        },
        "providers": [{"name": "markdown", "roles": ["source"]}],
    },
    "sanka/csv": {
        "kind": "connector",
        "protocol_version": "sanka-connector/v1",
        "distribution": {
            "name": "sanka-connector-csv",
            "version": "0.1.0a13",
            "entry_point": "csv",
        },
        "providers": [{"name": "csv", "roles": ["source"]}],
    },
    "sanka/sqlite": {
        "kind": "connector",
        "protocol_version": "sanka-connector/v1",
        "distribution": {
            "name": "sanka-connector-sqlite",
            "version": "0.1.0a13",
            "entry_point": "sqlite",
        },
        "providers": [{"name": "sqlite", "roles": ["source", "destination"]}],
    },
    "sanka/postgres": {
        "kind": "connector",
        "protocol_version": "sanka-connector/v1",
        "distribution": {
            "name": "sanka-connector-postgres",
            "version": "0.1.0a13",
            "entry_point": "postgres",
        },
        "providers": [{"name": "postgres", "roles": ["source", "destination"]}],
    },
    "sanka/clickhouse": {
        "kind": "connector",
        "protocol_version": "sanka-connector/v1",
        "distribution": {
            "name": "sanka-connector-clickhouse",
            "version": "0.1.0a13",
            "entry_point": "clickhouse",
        },
        "providers": [{"name": "clickhouse", "roles": ["destination"]}],
    },
}


def test_official_marketplace_has_system_access_and_code_conversion() -> None:
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


def test_release_workflow_uses_exact_source_and_the_staged_publisher() -> None:
    workflow = yaml.safe_load(Path(".github/workflows/publish.yml").read_text())
    release_steps = workflow["jobs"]["release"]["steps"]
    assert release_steps[0]["uses"].startswith("actions/checkout@")
    assert release_steps[0]["with"]["ref"] == "${{ github.sha }}"
    assert release_steps[1]["with"]["python-version"] == "3.12"
    download = next(
        step
        for step in release_steps
        if step.get("uses", "").startswith("actions/download-artifact@")
    )
    assert download["with"]["path"] == "."
    command = next(
        step["run"] for step in release_steps if "scripts/publish_release.py" in step.get("run", "")
    )
    assert '--tag "$GITHUB_REF_NAME" --revision "$GITHUB_SHA" --dist dist --publish' in command
    assert not any("gh release create" in step.get("run", "") for step in release_steps)
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_flow_supplement_uses_static_capabilities_and_a_complete_sdk_only_closure() -> None:
    from sanka_extension_sales_quote.metadata import CAPABILITY

    from sanka_extensions.flow import FlowCapability

    catalog = json.loads(Path("flow-marketplace.json").read_text())
    assert catalog == {
        "schema_version": "sanka-marketplace/v1",
        "extensions": [
            {
                "id": "sanka/sales-quote",
                "manifest": "packages/sanka-extension-sales-quote/extension.json",
            }
        ],
    }
    manifest = json.loads(Path(catalog["extensions"][0]["manifest"]).read_text())
    assert manifest["kind"] == "flow"
    assert manifest["protocol_version"] == "sanka-flow-extension/v1"
    assert manifest["commands"] == ["blueprint"]
    assert manifest["runtime"] == {"sanka_cli": ">=0.2.10,<0.3"}
    assert [FlowCapability.from_dict(item) for item in manifest["capabilities"]] == [CAPABILITY]
    assert {wheel["name"] for wheel in manifest["wheels"]} == {
        "sanka_connector_sdk-0.1.0a12-py3-none-any.whl",
        "sanka_extension_sdk-0.1.0a3-py3-none-any.whl",
        "sanka_extension_sales_quote-0.1.0a1-py3-none-any.whl",
    }
    assert all(wheel["url"].startswith(RELEASE_PREFIX) for wheel in manifest["wheels"])
    assert all(len(wheel["sha256"]) == 64 for wheel in manifest["wheels"])

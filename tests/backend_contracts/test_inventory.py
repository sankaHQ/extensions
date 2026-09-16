# SPDX-License-Identifier: Apache-2.0
"""Validate the shared backend migration fixture inventory."""

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = Path(__file__).with_name("capabilities.json")

REQUIRED_CAPABILITIES = {
    "access.member",
    "access.owner",
    "field.boolean",
    "field.large_integer",
    "field.uuid",
    "listing.cursor",
    "listing.ordering",
    "listing.search",
    "negative.custom_migration_operation",
    "negative.hook",
    "negative.middleware",
    "negative.raw_sql",
    "request.form_upload",
    "routing.apiview",
    "routing.format_suffix_alias",
    "routing.generic_crud",
    "routing.viewset_crud",
    "write.nested_crud",
}


def _test_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def test_inventory_covers_required_capabilities_and_existing_corpus() -> None:
    payload = json.loads(INVENTORY.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "sanka/backend-contract-fixtures/v1"
    assert payload["license"] == "Apache-2.0"

    fixtures = payload["fixtures"]
    assert len({fixture["id"] for fixture in fixtures}) == len(fixtures)
    covered = {capability for fixture in fixtures for capability in fixture["capabilities"]}
    assert covered >= REQUIRED_CAPABILITIES

    for fixture in fixtures:
        assert fixture["profile"] in payload["profiles"]
        fixture_path = ROOT / fixture["fixture"]
        assert fixture_path.exists(), fixture["id"]

        check_path_text, test_name = fixture["required_check"].split("::", 1)
        check_path = ROOT / check_path_text
        assert check_path.is_file(), fixture["required_check"]
        assert test_name in _test_functions(check_path), fixture["required_check"]

        source = fixture["source_success"]
        assert 200 <= source["status"] < 400, fixture["id"]
        assert source["status"] != 404, fixture["id"]
        assert source["database_effect"], fixture["id"]

        if any(capability.startswith("negative.") for capability in fixture["capabilities"]):
            assert fixture["expected_disposition"] == "blocking_gap"
            assert fixture["variant"]

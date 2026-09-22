# SPDX-License-Identifier: Apache-2.0
"""Scenario and observation contracts: validation, defaults, comparison."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sanka_http_replay import (
    ObservationError,
    ScenarioError,
    cases_document,
    compare,
    default_scenarios,
    difference,
    load_scenarios,
    validate_observations,
    validate_scenarios,
)

WIDGET = {
    "name": "Widget",
    "table": "widgets",
    "fields": [
        {
            "name": "id",
            "type": "integer",
            "nullable": False,
            "primary_key": True,
            "auto": True,
            "unique": False,
        },
        {
            "name": "name",
            "type": "string",
            "nullable": False,
            "primary_key": False,
            "auto": False,
            "unique": True,
        },
        {
            "name": "count",
            "type": "integer",
            "nullable": False,
            "primary_key": False,
            "auto": False,
            "unique": False,
        },
        {
            "name": "enabled",
            "type": "boolean",
            "nullable": False,
            "primary_key": False,
            "auto": False,
            "unique": False,
        },
        {
            "name": "note",
            "type": "string",
            "nullable": True,
            "primary_key": False,
            "auto": False,
            "unique": False,
        },
    ],
}
OPERATIONS = [
    {"method": "GET", "path": "/widgets", "kind": "list", "model": "Widget", "status": 200},
    {"method": "POST", "path": "/widgets", "kind": "create", "model": "Widget", "conflict": True},
    {"method": "GET", "path": "/widgets/:id", "kind": "lookup", "model": "Widget"},
    {"method": "PATCH", "path": "/widgets/:id", "kind": "update", "model": "Widget"},
    {"method": "PUT", "path": "/widgets/:id", "kind": "replace", "model": "Widget"},
    {"method": "DELETE", "path": "/widgets/:id", "kind": "delete", "model": "Widget"},
    {"method": "GET", "path": "/health", "kind": "literal", "status": 200},
]


def test_scenario_document_validation(tmp_path: Path) -> None:
    hosted = {
        "scenarios": [
            {"id": "list", "method": "GET", "path": "/api/items/", "expected_source_status": 200}
        ]
    }
    assert validate_scenarios(hosted) == [
        {
            "id": "list",
            "method": "GET",
            "path": "/api/items/",
            "headers": {},
            "expected_status": 200,
        }
    ]
    path = tmp_path / "sanka-verify.json"
    path.write_text(json.dumps(hosted))
    assert load_scenarios(path)[0]["id"] == "list"
    document = cases_document(validate_scenarios(hosted))
    assert document["schema"] == "sanka.http-scenarios/v1"
    for broken in (
        [],
        [{"id": "", "method": "GET", "path": "/"}],
        [{"id": "a", "method": "FETCH", "path": "/"}],
        [{"id": "a", "method": "GET", "path": "relative"}],
        [{"id": "a", "method": "GET", "path": "/", "body": {"x": 1}}],
        [{"id": "a", "method": "POST", "path": "/", "headers": {"Content-Type": "text/plain"}}],
        [{"id": "a", "method": "POST", "path": "/", "headers": {"A": "1", "a": "2"}}],
        [{"id": "a", "method": "GET", "path": "/"}, {"id": "a", "method": "GET", "path": "/"}],
        [{"id": "a", "method": "GET", "path": "/", "expected_status": 99}],
        [{"id": "a", "method": "GET", "path": "/", "setup": []}],
        [{"id": "a", "method": "GET", "path": "/", "extra": True}],
        {
            "schema": "sanka.http-scenarios/v2",
            "scenarios": [{"id": "a", "method": "GET", "path": "/"}],
        },
        {"scenarios": [{"id": "a", "method": "GET", "path": "/"}], "notes": "x"},
    ):
        with pytest.raises(ScenarioError):
            validate_scenarios(broken)
    with pytest.raises(ScenarioError):
        validate_scenarios([{"id": "a", "method": "POST", "path": "/", "body": "x" * 70_000}])


def test_default_scenarios_cover_the_write_contract() -> None:
    scenarios = default_scenarios(OPERATIONS, [WIDGET])
    ids = [scenario["id"] for scenario in scenarios]
    assert ids == [
        "Widget.create.missing",
        "Widget.create.wrong-type",
        "Widget.create.unknown-key",
        "Widget.create.first",
        "Widget.create.duplicate",
        "Widget.lookup.first",
        "Widget.lookup.invalid-id",
        "Widget.lookup.missing",
        "Widget.update.partial",
        "Widget.update.empty",
        "Widget.update.clear",
        "Widget.update.null-required",
        "Widget.update.unknown-key",
        "Widget.update.missing",
        "Widget.replace.missing-field",
        "Widget.replace.first",
        "Widget.replace.missing",
        "Widget.delete.first",
        "Widget.delete.again",
        "Widget.delete.invalid-id",
        "Widget.create.after",
        "get.widgets",
        "get.health",
    ]
    by_id = {scenario["id"]: scenario for scenario in scenarios}
    assert by_id["Widget.create.first"]["body"] == {
        "name": "alpha",
        "count": 7,
        "enabled": True,
        "note": None,
    }
    assert by_id["Widget.create.duplicate"]["expected_status"] == 409
    assert by_id["Widget.update.clear"]["body"] == {"note": None}
    assert by_id["Widget.update.null-required"]["body"] == {"name": None}
    assert by_id["Widget.delete.first"]["expected_status"] == 204
    assert by_id["Widget.create.after"]["body"]["name"] == "alpha3"
    assert by_id["get.widgets"]["method"] == "GET"
    # Deterministic and stable across calls.
    assert default_scenarios(OPERATIONS, [WIDGET]) == scenarios
    # A read-only contract yields one GET per route.
    reads = default_scenarios([OPERATIONS[0], OPERATIONS[-1]], [WIDGET])
    assert [scenario["path"] for scenario in reads] == ["/widgets", "/health"]
    for broken_model in (
        {**WIDGET, "fields": [dict(field, primary_key=False) for field in WIDGET["fields"]]},
        {**WIDGET, "fields": [dict(WIDGET["fields"][0], type="uuid")]},
    ):
        with pytest.raises(ScenarioError):
            default_scenarios(OPERATIONS, [broken_model])
    with pytest.raises(ScenarioError):
        default_scenarios(
            [{"method": "POST", "path": "/x", "kind": "create", "model": "Nope"}], [WIDGET]
        )
    with pytest.raises(ScenarioError):
        default_scenarios([OPERATIONS[1], OPERATIONS[1]], [WIDGET])


def test_observations_and_comparison() -> None:
    scenarios = validate_scenarios(
        [
            {
                "id": "create",
                "method": "POST",
                "path": "/w",
                "body": {"name": "a"},
                "expected_status": 201,
            },
            {"id": "delete", "method": "DELETE", "path": "/w/1", "expected_status": 204},
        ]
    )
    candidate = validate_observations(
        [
            {
                "status": 201,
                "media_type": "application/json; charset=utf-8",
                "body": {"id": 1, "name": "a"},
                "tables": {"w": [{"id": 1, "name": "a"}]},
                "sequences": {"w": ["1", True]},
            },
            {
                "id": "delete",
                "status": 204,
                "media_type": "",
                "body": None,
                "tables": {"w": []},
                "sequences": {"w": ["1", True]},
            },
        ],
        scenarios,
    )
    assert candidate[0]["media_type"] == "application/json"
    assert candidate[0]["id"] == "create" and candidate[0]["method"] == "POST"
    source = json.loads(json.dumps(candidate))
    report = compare(scenarios, candidate, source)
    assert report["ok"] is True
    assert [step["problems"] for step in report["steps"]] == [[], []]
    source[0]["tables"]["w"][0]["name"] = "tampered"
    report = compare(scenarios, candidate, source)
    assert report["ok"] is False
    assert report["steps"][0]["problems"] == [
        'source != candidate at $.tables.w[0].name: "tampered" != "a"'
    ]
    wrong_status = json.loads(json.dumps(candidate))
    wrong_status[1]["status"] = 200
    wrong_status[1]["media_type"] = "text/html"
    report = compare(scenarios, wrong_status)
    assert report["steps"][1]["problems"] == [
        "status 200 != expected 204",
        "candidate: media type 'text/html' is not JSON",
    ]
    assert difference(1, 1.0) == "$: 1 != 1.0"
    assert difference(True, 1) == "$: true != 1"
    assert difference({"a": [1, 2]}, {"a": [1, 3]}) == "$.a[1]: 2 != 3"
    assert difference({"a": 1}, {"b": 1}) == "$.a: missing on the candidate side"
    for broken in (
        [{"status": 201, "media_type": "application/json", "body": {}}],
        [
            {"id": "delete", "status": 201, "media_type": "application/json", "body": {}},
            {"status": 204, "media_type": "", "body": None},
        ],
        [
            {"status": "201", "media_type": "application/json", "body": {}},
            {"status": 204, "media_type": "", "body": None},
        ],
        [
            {"status": 201, "media_type": "application/json", "body": {}, "extra": 1},
            {"status": 204, "media_type": "", "body": None},
        ],
        {"schema": "sanka.http-observations/v9", "observations": []},
    ):
        with pytest.raises(ObservationError):
            validate_observations(broken, scenarios)


@pytest.mark.parametrize("media_type", ["", "application/json", "text/html"])
def test_bodyless_source_headers_remain_observable(media_type: str) -> None:
    scenarios = validate_scenarios(
        [{"id": "delete", "method": "DELETE", "path": "/w/1", "expected_status": 204}]
    )
    observed = validate_observations(
        [{"status": 204, "body": None, "media_type": media_type}], scenarios
    )
    assert compare(scenarios, observed, observed)["ok"]
    changed = [dict(observed[0], media_type="different")]
    assert not compare(scenarios, changed, observed)["ok"]
    assert not compare(scenarios, [dict(observed[0], body={})])["ok"]


def test_single_required_field_is_actually_missing() -> None:
    model = dict(WIDGET, fields=WIDGET["fields"][:2])
    scenarios = default_scenarios(OPERATIONS, [model])
    assert next(s for s in scenarios if s["id"] == "Widget.create.missing")["body"] == {}
    assert next(s for s in scenarios if s["id"] == "Widget.replace.missing-field")["body"] == {}

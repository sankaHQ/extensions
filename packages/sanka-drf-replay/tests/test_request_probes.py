# SPDX-License-Identifier: Apache-2.0
"""Request-derived replay probes when static capture cannot describe a view."""

import copy

import pytest

from sanka_drf_replay.replay import edge_probes_from_scan


@pytest.mark.parametrize("path", ["/records/7/", "/records/999999?expand=items"])
def test_uncaptured_numeric_get_probes_preserve_request_context(path):
    scenario = {
        "id": "detail",
        "method": "GET",
        "path": path,
        "headers": {"Authorization": "Custom sample"},
        "setup": [{"method": "POST", "path": "/fixtures/"}],
        "expected_source_status": 200,
    }
    before = copy.deepcopy(scenario)
    probes = edge_probes_from_scan({"routes": []}, [scenario])
    assert len(probes) == 1
    probe = probes[0]
    assert probe["path"] != path
    assert probe["path"].partition("?")[2] == path.partition("?")[2]
    assert probe["path"].partition("?")[0].endswith("/") == path.partition("?")[0].endswith("/")
    assert probe["headers"] == scenario["headers"]
    assert probe["setup"] == scenario["setup"]
    assert probe["method"] == "GET" and "expected_source_status" not in probe
    assert scenario == before


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/records/"),
        ("GET", "/records/slug/"),
        ("GET", "/v2/records/"),
        ("GET", "/records/7//"),
        ("GET", "/records/\u0667/"),
        ("DELETE", "/records/7/"),
    ],
)
def test_request_path_probes_do_not_guess_opaque_ids_or_mutate_writes(method, path):
    assert (
        edge_probes_from_scan({"routes": []}, [{"id": "sample", "method": method, "path": path}])
        == []
    )


def test_supplied_write_requests_cover_null_collections_and_text_without_static_routes():
    scenarios = [
        {
            "id": "create",
            "method": "POST",
            "path": "/records/",
            "body": {"name": "sample", "entries": [{"code": "A"}]},
            "headers": {"Authorization": "Custom sample"},
            "setup": [{"method": "POST", "path": "/fixtures/", "body": {"ready": True}}],
            "expected_source_status": 201,
        },
        {
            "id": "edit",
            "method": "PATCH",
            "path": "/records/7/",
            "body": {"entries": [{"code": "B"}]},
        },
    ]
    before = copy.deepcopy(scenarios)
    probes = edge_probes_from_scan({"routes": []}, scenarios)
    assert any(p["method"] == "PATCH" and p["body"] == {"entries": None} for p in probes)
    for value in ("", " \t ", " sample "):
        probe = next(p for p in probes if p.get("body", {}).get("name") == value)
        assert probe["headers"] == scenarios[0]["headers"]
        assert probe["setup"] == scenarios[0]["setup"]
        assert "expected_source_status" not in probe
    assert scenarios == before
    assert all(p["path"] in {s["path"] for s in scenarios} for p in probes)


def test_probe_budget_does_not_let_first_request_starve_later_write_methods():
    scenarios = [
        {
            "id": "wide",
            "method": "POST",
            "path": "/records/",
            "body": {f"field{i}": "value" for i in range(20)},
        },
        {"id": "partial", "method": "PATCH", "path": "/records/7/", "body": {"entries": []}},
        {"id": "read", "method": "GET", "path": "/records/7/"},
    ]
    probes = edge_probes_from_scan({"routes": []}, scenarios)
    assert len(probes) <= 12
    assert any(p["method"] == "PATCH" and p["body"] == {"entries": None} for p in probes)
    assert any(p["method"] == "POST" for p in probes)
    assert any(p["method"] == "GET" and p["path"] != "/records/7/" for p in probes)


def test_decimal_request_probes_cover_precision_without_changing_scale():
    scenario = {
        "id": "payment",
        "method": "POST",
        "path": "/payments/",
        "body": {"payer": 1, "payee": 2, "value": "12.50", "reference": "sample"},
        "headers": {"Authorization": "Custom sample"},
        "setup": [{"method": "POST", "path": "/fixtures/"}],
    }
    before = copy.deepcopy(scenario)
    probes = edge_probes_from_scan({"routes": []}, [scenario])
    precision = [
        p
        for p in probes
        if isinstance(p.get("body", {}).get("value"), str)
        and len(p["body"]["value"].split(".")[0]) > 18
    ]
    assert len(precision) == 1
    probe = precision[0]
    assert probe["body"]["value"].split(".")[1] == "50"
    assert probe["body"]["reference"] == "sample"
    assert probe["headers"] == scenario["headers"]
    assert probe["setup"] == scenario["setup"]
    assert len(probes) <= 12
    assert scenario == before

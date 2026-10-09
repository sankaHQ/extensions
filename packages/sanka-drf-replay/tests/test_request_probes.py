# SPDX-License-Identifier: Apache-2.0
"""Request-derived replay probes when static capture cannot describe a view."""

import copy

from sanka_drf_replay.replay import edge_probes_from_scan


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
    ]
    probes = edge_probes_from_scan({"routes": []}, scenarios)
    assert len(probes) <= 12
    assert any(p["method"] == "PATCH" and p["body"] == {"entries": None} for p in probes)
    assert any(p["method"] == "POST" for p in probes)

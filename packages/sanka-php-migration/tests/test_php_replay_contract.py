# SPDX-License-Identifier: Apache-2.0
"""Replay admission owns coverage and immutable candidate dependencies."""

from pathlib import Path

import pytest
from sanka_php_migration.replay import route_coverage, snapshot


def test_scenarios_cannot_leave_selected_routes_untested_or_enter_excluded_routes() -> None:
    routes = [{"method": "GET", "path": "/orders/{id}"}, {"method": "POST", "path": "/orders"}]
    cases = [{"id": "lookup", "method": "GET", "path": "/orders/1?owner=a", "expected_status": 200}]
    with pytest.raises(ValueError):
        route_coverage(routes, cases)
    cases.append({"id": "create", "method": "POST", "path": "/orders", "expected_status": 201})
    assert route_coverage(routes, cases) == {
        "GET /orders/{id}": ["lookup"],
        "POST /orders": ["create"],
    }
    cases.append({"id": "excluded", "method": "GET", "path": "/admin", "expected_status": 200})
    with pytest.raises(ValueError):
        route_coverage(routes, cases)


def test_candidate_snapshot_binds_bytes_and_rejects_symlinks(tmp_path: Path) -> None:
    (tmp_path / "handlers.py").write_text("first")
    before = snapshot(tmp_path)
    (tmp_path / "handlers.py").write_text("second")
    assert snapshot(tmp_path) != before
    (tmp_path / "link").symlink_to(tmp_path / "handlers.py")
    with pytest.raises(ValueError):
        snapshot(tmp_path)

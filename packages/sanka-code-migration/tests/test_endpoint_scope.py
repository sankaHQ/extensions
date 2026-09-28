# SPDX-License-Identifier: Apache-2.0
"""Ownership is checked before cumulative generation can replace files."""

import pytest
from sanka_code_migration.endpoints import apply_files, plan_scope


def test_cumulative_scope_preserves_repairs_and_unowned_files(tmp_path):
    artifacts, output = tmp_path / ".sanka", tmp_path / "candidate"
    routes = [{"method": "GET", "path": "/one"}, {"method": "POST", "path": "/two"}]

    def plan(selected):
        return plan_scope(
            routes,
            selected,
            artifacts=artifacts,
            output=output,
            target="test",
            context={"auth": "required"},
        )

    first = plan(["GET /one"])
    apply_files(artifacts, first, {"app.py": "one"}, "reviewed-first")
    (output / "notes.txt").write_text("user-owned")
    with pytest.raises(ValueError):
        plan(["POST /two"])
    second = plan(["GET /one", "POST /two"])
    assert second["retained_ids"] == ["GET /one"]
    assert second["endpoints"][0]["locked"]
    apply_files(artifacts, second, {"app.py": "one and two"}, "reviewed-second")
    assert (output / "app.py").read_text() == "one and two"
    assert (output / "notes.txt").read_text() == "user-owned"
    with pytest.raises(ValueError):
        apply_files(artifacts, second, {"app.py": "stale"}, "stale")
    reviewed = plan(None)
    (output / "app.py").write_text("user repair")
    with pytest.raises(ValueError):
        apply_files(artifacts, reviewed, {"app.py": "replacement"}, "reviewed")
    with pytest.raises(ValueError):
        plan(None)
    assert (output / "app.py").read_text() == "user repair"


@pytest.mark.parametrize("selection", [[], ["GET /missing"], ["GET /one"] * 2, "GET /one", [{}]])
def test_invalid_scope_cannot_become_whole_project(tmp_path, selection):
    with pytest.raises(ValueError):
        plan_scope(
            [{"method": "GET", "path": "/one"}],
            selection,
            artifacts=tmp_path / ".sanka",
            output=tmp_path / "out",
            target="test",
            context={},
        )

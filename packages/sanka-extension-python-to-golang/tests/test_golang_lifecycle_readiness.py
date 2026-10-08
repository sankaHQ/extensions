# SPDX-License-Identifier: Apache-2.0
"""Readiness failures stay distinct from execution faults at the extension boundary."""

import dataclasses
from pathlib import Path

import pytest
from sanka_extension_python_to_golang.adapter import handle
from test_golang_drf_project import gadget_project
from test_python_to_golang import request, source


@pytest.mark.parametrize("command", ["apply", "test", "verify"])
def test_unsupported_capture_returns_gaps_without_generation(tmp_path: Path, command: str):
    (tmp_path / "app.py").write_text(source("flask") + "\nunknown_startup()\n")
    req = request(tmp_path)
    planned = handle(req)
    assert planned.outcome == "success", planned.error
    gaps = planned.data["capture"]["gaps"]
    assert gaps
    result = handle(dataclasses.replace(req, command=command))
    assert result.outcome == "error"
    assert result.error.code == "SANKA_EXTENSION_READINESS"
    assert result.error.details["gaps"] == gaps
    assert not (Path(req.artifact_root) / "golang").exists()


@pytest.mark.parametrize("command", ["test", "verify"])
def test_missing_replay_input_invalidates_old_report(tmp_path: Path, command: str):
    config = gadget_project(tmp_path)
    (tmp_path / "sanka-verify.json").unlink()
    req = dataclasses.replace(request(tmp_path, "drf"), configuration=config)
    planned = handle(req)
    assert planned.outcome == "success", planned.error
    applied = handle(
        dataclasses.replace(
            req,
            command="apply",
            reviewed_plan_hash="reviewed",
            configuration=config | {"extension_plan_hash": planned.data["plan_hash"]},
        )
    )
    assert applied.outcome == "success", applied.error
    report = Path(req.artifact_root) / f"{command}.json"
    report.write_text('{"ok": true}')
    result = handle(dataclasses.replace(req, command=command))
    assert result.outcome == "error"
    assert result.error.code == "SANKA_EXTENSION_INPUT_REQUIRED"
    assert result.error.details["files"] == ["sanka-verify.json"]
    assert not report.exists()

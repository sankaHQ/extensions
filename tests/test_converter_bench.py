# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import runpy
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from sanka_extension_sdk import ExtensionRequest, encode_response, success_response

# The pytest executable need not add the repository root to sys.path.
gate = SimpleNamespace(
    **runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/run_converter_bench.py"))
)


def test_route_floor_rejects_downgrades_and_fixture_drift() -> None:
    envelope = {"task": [7, 13]}
    assert (
        gate.readiness_error(
            "task",
            {
                "native_routes": 7,
                "native_eligible_routes": 13,
                "readiness": 7 / 13,
            },
            envelope,
        )
        is None
    )
    for plan in (
        {"native_routes": 6, "native_eligible_routes": 13, "readiness": 6 / 13},
        {"native_routes": 7, "native_eligible_routes": 14, "readiness": 7 / 14},
        {"native_routes": 7, "native_eligible_routes": 13, "readiness": 1.0},
    ):
        assert gate.readiness_error("task", plan, envelope)
    assert gate.readiness_error("new-task", {}, envelope)


def test_full_readiness_cannot_override_failed_native_gate() -> None:
    result = {
        "status": "passed",
        "fully_migrated": True,
        "hard_gates": {
            "source_qualified": True,
            "native_target": False,
            "behavior_parity": True,
        },
    }
    assert gate.evaluation_error(7, 7, result)


def test_partial_candidate_requires_qualified_source_and_stable_execution() -> None:
    result = {
        "status": "failed",
        "fully_migrated": False,
        "hard_gates": {
            "source_qualified": True,
            "target_boot": True,
            "deterministic": True,
            "behavior_parity": False,
        },
    }
    baseline = {
        "required_gates": ["source_qualified", "target_boot", "deterministic"],
        "metrics": {"behavioral_parity": [5, 17]},
    }
    result["metrics"] = {"behavioral_parity": {"passed": 5, "total": 17}}
    assert gate.evaluation_error(7, 13, result, baseline) is None
    assert gate.evaluation_error(7, 13, result)
    for key in baseline["required_gates"]:
        invalid = {**result, "hard_gates": {**result["hard_gates"], key: False}}
        assert gate.evaluation_error(7, 13, invalid, baseline)
    for metrics in (
        {},
        {"behavioral_parity": {"passed": 0, "total": 17}},
        {"behavioral_parity": {"passed": 5, "total": 16}},
    ):
        # Deterministic wrong/404 responses cannot satisfy the old baseline.
        assert gate.evaluation_error(7, 13, {**result, "metrics": metrics}, baseline)


def test_pinned_benchmark_source_wins_over_current_directory(tmp_path: Path) -> None:
    bench = tmp_path / "bench"
    pinned = bench / "src/sanka_bench/__init__.py"
    pinned.parent.mkdir(parents=True)
    pinned.write_text("")
    shadow = tmp_path / "sanka_bench/__init__.py"
    shadow.parent.mkdir()
    shadow.write_text('raise RuntimeError("unreviewed evaluator")')
    module = subprocess.check_output(
        [sys.executable, "-P", "-c", "import sanka_bench; print(sanka_bench.__file__)"],
        cwd=tmp_path,
        env=gate.benchmark_environment(bench),
        text=True,
    ).strip()
    assert Path(module) == pinned


@pytest.mark.parametrize("wrong_identity,returncode", [(True, 0), (False, 1)])
def test_stdio_rejects_wrong_response_identity_or_exit_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wrong_identity: bool,
    returncode: int,
) -> None:
    request = ExtensionRequest(
        request_id="scan-1",
        command="scan",
        project_root=str(tmp_path),
        artifact_root=str(tmp_path),
        extension_id="sanka/drf-to-fastapi",
        extension_version="0.1.0a3",
        manifest_digest="a" * 64,
        fingerprint={},
        configuration={},
        prior_artifacts=(),
        reviewed_plan_hash=None,
    )
    response = success_response(request, data={})
    if wrong_identity:
        response = replace(response, request_id="other-request")
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            returncode,
            json.dumps(encode_response(response)),
            "",
        ),
    )
    with pytest.raises(ValueError, match="identity or exit status"):
        gate.invoke(Path("python"), request, {})


def test_environment_does_not_pass_operator_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANKA_API_KEY", "must-not-reach-fixtures")
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "customer.settings")
    monkeypatch.setenv("PYTHONPATH", "/unreviewed/modules")
    environment = gate.clean_environment()
    assert "SANKA_API_KEY" not in environment
    assert "DJANGO_SETTINGS_MODULE" not in environment
    assert "PYTHONPATH" not in environment


def test_private_report_cannot_be_written_into_public_repo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = gate.ROOT / "private-converter-report.json"
    monkeypatch.setattr(
        gate.sys,
        "argv",
        [
            "run_converter_bench.py",
            "--bench-dir",
            str(tmp_path),
            "--output",
            str(output),
        ],
    )
    with pytest.raises(SystemExit) as error:
        gate.main()
    assert error.value.code == 2
    assert not output.exists()

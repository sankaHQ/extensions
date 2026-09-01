# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sanka_extension_drf_to_fastapi import __main__, adapter
from sanka_extension_sdk import ExtensionRequest, JsonValue, encode_request


def request_for(
    tmp_path: Path,
    *,
    command: str,
    configuration: dict[str, JsonValue] | None = None,
    reviewed_plan_hash: str | None = None,
) -> ExtensionRequest:
    project_root = tmp_path / "project"
    artifact_root = tmp_path / "artifacts"
    project_root.mkdir(exist_ok=True)
    artifact_root.mkdir(exist_ok=True)
    return ExtensionRequest(
        request_id="request-1",
        command=command,
        project_root=str(project_root.resolve()),
        artifact_root=str(artifact_root.resolve()),
        extension_id="sanka/drf-to-fastapi",
        extension_version="0.1.0a1",
        manifest_digest="0" * 64,
        fingerprint={},
        configuration={} if configuration is None else configuration,
        prior_artifacts=(),
        reviewed_plan_hash=reviewed_plan_hash,
    )


def test_scan_adapter_echoes_identity_and_namespaces_artifacts(tmp_path: Path) -> None:
    request = request_for(
        tmp_path,
        command="scan",
        configuration={"settings_module": "config.settings"},
    )
    with patch.object(adapter, "scan_django") as scan:
        scan.return_value.to_dict.return_value = {"scan_hash": "sha256:scan", "risks": []}
        response = adapter.handle(request)

    assert response.request_id == request.request_id
    assert response.extension_id == "sanka/drf-to-fastapi"
    assert response.data["scan_hash"] == "sha256:scan"
    assert response.artifacts == (str(Path(request.artifact_root) / "scan.json"),)
    scan.assert_called_once_with(
        request.project_root,
        settings_module="config.settings",
        artifact_dir=request.artifact_root,
    )


def test_adapter_rejects_unsupported_command(tmp_path: Path) -> None:
    response = adapter.handle(request_for(tmp_path, command="publish"))

    assert response.outcome == "error"
    assert response.error is not None
    assert response.error.code == "SANKA_EXTENSION_UNSUPPORTED_COMMAND"


def test_plan_reports_missing_required_configuration(tmp_path: Path) -> None:
    response = adapter.handle(request_for(tmp_path, command="plan", configuration={}))

    assert response.outcome == "error"
    assert response.error is not None
    assert response.error.code == "SANKA_EXTENSION_INPUT_REQUIRED"
    assert response.error.details == {
        "inputs": ["generation", "output", "package_manager", "strategy"]
    }


def test_apply_rejects_mismatched_reviewed_extension_plan_hash(tmp_path: Path) -> None:
    with patch.object(
        adapter,
        "load_fastapi_plan",
        return_value=SimpleNamespace(plan_hash="sha256:current"),
    ):
        response = adapter.handle(
            request_for(
                tmp_path,
                command="apply",
                configuration={"extension_plan_hash": "sha256:stale"},
                reviewed_plan_hash="sha256:core-plan",
            )
        )

    assert response.outcome == "error"
    assert response.error is not None
    assert response.error.code == "SANKA_EXTENSION_PLAN_HASH_MISMATCH"


def test_entrypoint_converts_exception_without_traceback_on_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = request_for(tmp_path, command="scan")
    stdin = io.StringIO(json.dumps(encode_request(request)))
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    with patch.object(__main__, "handle", side_effect=RuntimeError("framework exploded")):
        assert __main__.main() == 1

    documents = stdout.getvalue().splitlines()
    assert len(documents) == 1
    payload = json.loads(documents[0])
    assert payload["outcome"] == "error"
    assert payload["error"]["code"] == "SANKA_EXTENSION_EXECUTION_FAILED"
    assert "Traceback" not in stdout.getvalue()

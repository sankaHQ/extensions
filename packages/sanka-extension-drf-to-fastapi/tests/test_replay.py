# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from adapter_cli import run_cli

from sanka_extension_drf_to_fastapi import adapter
from sanka_extension_drf_to_fastapi import replay as replay_module
from sanka_extension_drf_to_fastapi.replay import (
    ReplayError,
    body_difference,
    diff_snapshots,
    edge_probes_from_scan,
    load_scenarios,
    normalize_body,
    replay,
    snapshot_database,
)
from sanka_extension_sdk import ExtensionRequest

FIXTURES = Path(__file__).parent / "fixtures"


def test_load_scenarios_accepts_the_bench_format_and_rejects_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "scenarios.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "list",
                    "method": "get",
                    "path": "/api/gadgets/",
                    "capture_headers": ["Allow"],
                },
                {
                    "id": "second-page",
                    "method": "GET",
                    "path": "/api/gadgets/?page=2",
                    "setup": [{"method": "GET", "path": "/api/gadgets/"}],
                },
            ]
        ),
        encoding="utf-8",
    )
    scenarios = load_scenarios(path)
    assert [item["id"] for item in scenarios] == ["list", "second-page"]
    assert scenarios[0]["method"] == "GET"
    assert scenarios[0]["capture_headers"] == ["allow"]
    assert scenarios[1]["setup"][0]["path"] == "/api/gadgets/"
    path.write_text(
        json.dumps(
            [
                {"id": "a", "method": "GET", "path": "/x/"},
                {"id": "a", "method": "GET", "path": "/y/"},
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ReplayError, match="duplicate scenario id"):
        load_scenarios(path)
    path.write_text(
        json.dumps([{"id": "bad", "method": "GET", "path": "no-slash"}]), encoding="utf-8"
    )
    with pytest.raises(ReplayError, match="must start with"):
        load_scenarios(path)


def test_edge_probes_cover_options_unsupported_method_slash_and_missing_object() -> None:
    scan = {
        "routes": [
            {"method": "GET", "path": "/api/gadgets/"},
            {"method": "POST", "path": "/api/gadgets/"},
            {"key": "GET /api/gadgets/{pk}/"},
            {"method": "DELETE", "path": "/api/gadgets/{pk}/"},
            {"method": "GET", "path": "/api/{owner}/{slug}/"},
        ]
    }
    probes = edge_probes_from_scan(scan)
    ids = [probe["id"] for probe in probes]
    assert "edge:options:OPTIONS /api/gadgets/" in ids
    assert "edge:method-not-allowed:TRACE /api/gadgets/" in ids
    assert "edge:slash-variant:GET /api/gadgets" in ids
    assert "edge:missing-object:GET /api/gadgets/999999/" in ids
    assert not any("owner" in probe["id"] for probe in probes)
    assert all(
        probe["capture_headers"] == ["allow", "location", "www-authenticate"] for probe in probes
    )
    assert all(probe["generated_from"] for probe in probes)


def test_snapshot_and_diff_report_row_differences(tmp_path: Path) -> None:
    left = tmp_path / "left.sqlite3"
    right = tmp_path / "right.sqlite3"
    for path, rows in ((left, [(1, "a"), (2, "b")]), (right, [(1, "a"), (2, "changed")])):
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE inventory_gadget (id INTEGER PRIMARY KEY, name TEXT)")
            connection.execute("CREATE TABLE django_session (id INTEGER PRIMARY KEY)")
            connection.executemany("INSERT INTO inventory_gadget VALUES (?, ?)", rows)
    source = snapshot_database(left, ("django_session",))
    candidate = snapshot_database(right, ("django_session",))
    assert set(source) == {"inventory_gadget"}
    differences = diff_snapshots(source, candidate)
    assert differences[0]["table"] == "inventory_gadget"
    assert differences[0]["kind"] == "rows"
    assert differences[0]["only_in_source"] == [[2, "b"]]
    assert diff_snapshots(source, source) == []


def test_body_normalization_and_difference_paths() -> None:
    assert normalize_body(b'{"a": [1, 2]}', "application/json", None) == {"a": [1, 2]}
    assert normalize_body(b"plain", "text/plain", None) == "plain"
    assert normalize_body(b"\xff\x00", "application/octet-stream", "base64") == {"base64": "/wA="}
    assert normalize_body(b"", "application/json", None) is None
    assert body_difference({"a": {"b": 1}}, {"a": {"b": 2}}) == "$.a.b: source=1 candidate=2"
    assert body_difference([1, 2], [1]) == "$: 2 items in source, 1 in candidate"
    assert body_difference({"x": 1}, {"x": 1}) is None


def _cli(project: Path, arguments: list[str]) -> None:
    """Drive the adapter CLI in a clean subprocess; in-process Django state is per-process."""
    completed = run_cli(arguments, project)
    assert completed.returncode == 0, (arguments, completed.stdout, completed.stderr)


def _generate_native_candidate(project: Path) -> Path:
    _cli(project, ["scan", str(project)])
    _cli(project, ["plan", str(project), "--to", "fastapi"])
    plan = json.loads((project / ".sanka" / "plan-fastapi.json").read_text(encoding="utf-8"))
    _cli(project, ["apply", "--root", str(project), "--plan-hash", plan["plan_hash"]])
    output = project / ".sanka" / "output" / "fastapi"
    manifest = json.loads((output / "sanka-manifest.json").read_text(encoding="utf-8"))
    assert (output / manifest["entrypoint"]).is_file()
    return output


CRUD_SCENARIOS = [
    {"id": "list-empty", "method": "GET", "path": "/api/gadgets/", "capture_headers": ["Allow"]},
    {
        "id": "create",
        "method": "POST",
        "path": "/api/gadgets/",
        "body": {"name": "Beta", "quantity": 2, "notes": ""},
    },
    {
        "id": "create-invalid",
        "method": "POST",
        "path": "/api/gadgets/",
        "body": {"name": "", "quantity": "x"},
    },
    {"id": "detail-missing", "method": "GET", "path": "/api/gadgets/404/"},
    {
        "id": "detail-after-create",
        "method": "GET",
        "path": "/api/gadgets/1/",
        "setup": [
            {
                "method": "POST",
                "path": "/api/gadgets/",
                "body": {"name": "Gamma", "quantity": 5, "notes": "n"},
            }
        ],
    },
    {
        "id": "delete-after-create",
        "method": "DELETE",
        "path": "/api/gadgets/1/",
        "setup": [
            {
                "method": "POST",
                "path": "/api/gadgets/",
                "body": {"name": "Delta", "quantity": 1, "notes": ""},
            }
        ],
    },
    {"id": "options", "method": "OPTIONS", "path": "/api/gadgets/", "capture_headers": ["Allow"]},
]


def test_replay_matches_the_generated_native_app_and_flags_a_regression(tmp_path: Path) -> None:
    project = tmp_path / "crud"
    shutil.copytree(FIXTURES / "drf_crud_project", project)
    output = _generate_native_candidate(project)
    manifest = json.loads((output / "sanka-manifest.json").read_text(encoding="utf-8"))
    scan_payload = json.loads((project / ".sanka" / "scan.json").read_text(encoding="utf-8"))
    edge_probes = replay_module.edge_probes_from_scan(scan_payload)
    assert any(probe["id"].startswith("edge:slash-variant:") for probe in edge_probes)
    report = replay(
        project,
        [
            replay_module._validated_request(item, item["id"], require_id=True)
            for item in CRUD_SCENARIOS
        ]
        + edge_probes,
        settings_module="crud_config.settings",
        candidate_root=output,
        entrypoint=str(manifest["entrypoint"]),
        db_env="SANKA_TEST_DB",
        python=Path(sys.executable),
    )
    assert report["summary"]["scenarios"] == len(CRUD_SCENARIOS) + len(edge_probes)
    # The generated native app answers OPTIONS and 405 exactly like DRF, so a fresh
    # candidate replays clean; the verifier must report no mismatch at all.
    baseline_failures = [item for item in report["scenarios"] if not item["match"]]
    assert baseline_failures == [], report["summary_lines"]
    assert report["ok"], report["summary_lines"]
    assert all(item["native"]["compliant"] for item in report["scenarios"])
    # Regress one exact DRF error string in the candidate and replay again.
    # The generated app reads DRF's exact error strings from its manifest message table.
    carriers = [
        path
        for path in sorted(output.rglob("*"))
        if path.suffix in {".py", ".json"}
        and "This field may not be blank." in path.read_text(encoding="utf-8")
    ]
    assert carriers, sorted(str(path.relative_to(output)) for path in output.rglob("*"))
    native_module = carriers[0]
    text = native_module.read_text(encoding="utf-8")
    native_module.write_text(
        text.replace("This field may not be blank.", "Blank!"), encoding="utf-8"
    )
    regressed = replay(
        project,
        [
            replay_module._validated_request(item, item["id"], require_id=True)
            for item in CRUD_SCENARIOS
        ],
        settings_module="crud_config.settings",
        candidate_root=output,
        entrypoint=str(manifest["entrypoint"]),
        db_env="SANKA_TEST_DB",
        python=Path(sys.executable),
    )
    assert not regressed["ok"]
    failing = {item["id"]: item for item in regressed["scenarios"] if not item["match"]}
    assert set(failing) == {"create-invalid"}
    assert failing["create-invalid"]["body_match"] is False
    assert "Blank!" in str(failing["create-invalid"]["body_difference"])
    assert any("create-invalid" in line for line in regressed["summary_lines"])


def _request(tmp_path: Path, configuration: dict[str, object]) -> ExtensionRequest:
    project_root = tmp_path / "project"
    artifact_root = tmp_path / "artifacts"
    project_root.mkdir(exist_ok=True)
    artifact_root.mkdir(exist_ok=True)
    return ExtensionRequest(
        request_id="request-replay",
        command="verify",
        project_root=str(project_root.resolve()),
        artifact_root=str(artifact_root.resolve()),
        extension_id="sanka/drf-to-fastapi",
        extension_version="0.1.0a1",
        manifest_digest="0" * 64,
        fingerprint={},
        configuration=configuration,  # type: ignore[arg-type]
        prior_artifacts=(),
        reviewed_plan_hash=None,
    )


def test_verify_with_scenarios_dispatches_to_replay_without_a_plan(tmp_path: Path) -> None:
    request = _request(
        tmp_path,
        {
            "scenarios": "public-tests/scenarios.json",
            "db_env": "BENCH_DB_PATH",
            "entrypoint": "target_app.py",
            "settings_module": "config.settings",
            "ignore_tables": ["django_session"],
            "all_headers": True,
        },
    )
    scenarios = Path(request.project_root) / "public-tests" / "scenarios.json"
    scenarios.parent.mkdir()
    scenarios.write_text(
        json.dumps([{"id": "list", "method": "GET", "path": "/api/x/"}]), encoding="utf-8"
    )
    report = {
        "schema": "sanka-verify-replay/v1",
        "ok": False,
        "summary": {"scenarios": 1, "matched": 0, "mismatched": 1},
        "scenarios": [],
        "summary_lines": ["0/1 scenarios match", "list [GET /api/x/]: status 200 vs 404"],
    }
    with patch.object(adapter, "replay", return_value=report) as run:
        response = adapter.handle(request)
    assert run.call_count == 1
    kwargs = run.call_args.kwargs
    assert kwargs["settings_module"] == "config.settings"
    assert kwargs["db_env"] == "BENCH_DB_PATH"
    assert kwargs["entrypoint"] == "target_app.py"
    assert kwargs["ignored_tables"] == ("django_session",)
    assert kwargs["all_headers"] is True
    assert kwargs["candidate_root"] == Path(request.project_root)
    assert response.outcome == "error"
    assert response.error is not None
    assert response.error.code == "SANKA_EXTENSION_REPLAY_MISMATCH"
    assert response.data["summary"]["mismatched"] == 1
    assert response.limitations[-1].startswith("list [GET /api/x/]")


def test_verify_with_scenarios_reports_invalid_scenario_files(tmp_path: Path) -> None:
    request = _request(tmp_path, {"scenarios": "missing.json"})
    response = adapter.handle(request)
    assert response.outcome == "error"
    assert response.error is not None
    assert response.error.code == "SANKA_EXTENSION_REPLAY_INVALID"


def test_default_interpreter_prefers_the_checkout_virtualenv(tmp_path: Path) -> None:
    fallback = Path(sys.executable)
    assert replay_module.default_interpreter(tmp_path, fallback) == fallback
    interpreter = tmp_path / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")
    assert replay_module.default_interpreter(tmp_path, fallback) == interpreter

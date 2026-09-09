# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import copy
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from adapter_cli import run_cli

from sanka_drf_replay import replay as replay_module
from sanka_drf_replay.replay import (
    ReplayError,
    body_difference,
    diff_snapshots,
    edge_probes_from_scan,
    load_scenarios,
    normalize_body,
    replay,
    snapshot_database,
)
from sanka_extension_drf_to_fastapi import adapter
from sanka_extension_sdk import ExtensionRequest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("target", ["fastapi", "flask"])
def test_replay_isolates_seed_media_and_rejects_relocated_uploads(tmp_path, target):
    project = tmp_path / "crud"
    shutil.copytree(FIXTURES / "drf_crud_project", project)
    (project / "crud_config/urls.py").write_text("""
from pathlib import Path
from django.conf import settings
from django.http import JsonResponse
from django.urls import path
def upload(request):
    root = Path(settings.MEDIA_ROOT)
    assert (root / "seed.txt").read_text() == "seed"
    assert not (root / "new.txt").exists()
    (root / "new.txt").write_text("new")
    return JsonResponse({"ok": True})
urlpatterns = [path("upload/", upload)]
""")
    seed = project / "seed.py"
    seed.write_text("""
from pathlib import Path
from django.conf import settings
Path(settings.MEDIA_ROOT, "seed.txt").write_text("seed")
""")
    app = project / "target_app.py"
    header = (
        'from fastapi import FastAPI\napp = FastAPI()\n@app.get("/upload/")\n'
        if target == "fastapi"
        else 'from flask import Flask\napp = Flask(__name__)\n@app.get("/upload/")\n'
    )
    implementation = """def upload():
    import os
    from pathlib import Path
    root = Path(os.environ["BENCH_MEDIA_ROOT"])
    assert (root / "seed.txt").read_text() == "seed"
    assert not (root / "new.txt").exists()
    DEST.write_text("new")
    return {"ok": True}
"""
    scenarios = [{"id": str(i), "method": "GET", "path": "/upload/"} for i in range(2)]
    for destination, expected in [
        ('(root / "new.txt")', True),
        ('(root.parent / "relocated.txt")', False),
    ]:
        app.write_text(header + implementation.replace("DEST", destination))
        report = replay(
            project,
            scenarios,
            settings_module="crud_config.settings",
            db_env="SANKA_TEST_DB",
            seed=seed,
            python=Path(sys.executable),
            target=target,
        )
        assert report["ok"] is expected
        assert report["summary"]["media_mismatches"] == (0 if expected else 2)
        assert all(row["body_match"] for row in report["scenarios"])
    assert not (project / "media").exists()
    seed.write_text('from django.conf import settings\nsettings.MEDIA_ROOT = "elsewhere"\n')
    with pytest.raises(ReplayError, match="seed changed MEDIA_ROOT"):
        replay(
            project,
            scenarios,
            settings_module="crud_config.settings",
            db_env="SANKA_TEST_DB",
            seed=seed,
            python=Path(sys.executable),
            target=target,
        )


def test_multipart_keeps_declared_boundary_and_binary_content():
    import base64

    scope = {"payload": {"boundary": "default"}}
    exec(replay_module._REQUEST_SCRIPT, scope)
    body, headers = scope["request_bytes"](
        {
            "multipart": {
                "boundary": "ExactBoundary",
                "files": [
                    {
                        "field": "file",
                        "filename": "x.bin",
                        "content_b64": base64.b64encode(b"prefix--ExactBoundary-suffix").decode(),
                    }
                ],
            }
        }
    )
    assert headers["content-type"].endswith("boundary=ExactBoundary")
    assert b"prefix--ExactBoundary-suffix" in body


def test_write_probes_keep_context_and_mutate_only_supplied_payloads():
    import base64

    supplied = [
        {
            "id": "write",
            "method": "PATCH",
            "path": "/records/4/",
            "headers": {"authorization": "Bearer fixture"},
            "setup": [{"method": "POST", "path": "/records/"}],
            "expected_source_status": 200,
            "body": {"records": [{"name": "a", "parts": [{"code": "b"}]}]},
        },
        {
            "id": "upload",
            "method": "POST",
            "path": "/uploads/",
            "multipart": {
                "boundary": "Sample-73",
                "fields": {"label": "original"},
                "files": [{"field": "blob", "filename": "original.bin", "content_b64": "YQ=="}],
            },
        },
    ]
    original = copy.deepcopy(supplied)
    scan = {"routes": [{"method": s["method"], "path": s["path"]} for s in supplied]}
    probes = {
        p["probe_kind"]: p for p in edge_probes_from_scan(scan, supplied) if p.get("probe_kind")
    }
    assert supplied == original
    assert len(probes["duplicate-record-0"]["body"]["records"]) == 2
    nested = probes["duplicate-record-1"]
    assert len(nested["body"]["records"][0]["parts"]) == 2
    assert nested["headers"] == supplied[0]["headers"]
    assert nested["setup"] == supplied[0]["setup"]
    assert "expected_source_status" not in nested
    upload = probes["upload-boundary"]["multipart"]
    assert upload["fields"] == {"label": "original"}
    assert upload["files"][0]["filename"] == "original.bin"
    assert b"--Sample-73" in base64.b64decode(upload["files"][0]["content_b64"])
    nested["body"]["records"][0]["parts"][0]["code"] = "changed"
    assert supplied == original
    assert nested["body"]["records"][0]["parts"][1]["code"] == "b"
    assert not replay_module._write_probes({"body": {"records": []}, "multipart": {"files": []}})


@pytest.mark.parametrize("target", ["fastapi", "flask"])
def test_write_probes_find_parser_and_nested_validation_divergence(tmp_path, target):
    project = tmp_path / "crud"
    shutil.copytree(FIXTURES / "drf_crud_project", project)
    (project / "crud_config/urls.py").write_text("""
import base64, json
from django.http import JsonResponse
from django.urls import path
from django.views.decorators.csrf import csrf_exempt
@csrf_exempt
def write(request):
    if request.content_type == "multipart/form-data":
        return JsonResponse({"bytes": base64.b64encode(request.FILES["blob"].read()).decode()})
    records = json.loads(request.body)["records"]
    if len({record["code"] for record in records}) != len(records):
        return JsonResponse({"records": ["duplicate"]}, status=400)
    return JsonResponse({"count": len(records)})
urlpatterns = [path("write/", write)]
""")
    app = """
import base64, json
from email.parser import BytesParser
from email.policy import default
def result(raw, content_type):
    if content_type.startswith("multipart/form-data"):
        message = BytesParser(policy=default).parsebytes(
            b"Content-Type: " + content_type.encode() + b"\\r\\n\\r\\n" + raw)
        part = next(message.iter_parts())
        return {"bytes": base64.b64encode(part.get_payload(decode=True)).decode()}
    return {"count": len(json.loads(raw)["records"])}
"""
    app += (
        """
from fastapi import FastAPI, Request
app = FastAPI()
@app.post("/write/")
async def write(request: Request):
    return result(await request.body(), request.headers["content-type"])
"""
        if target == "fastapi"
        else """
from flask import Flask, request
app = Flask(__name__)
@app.post("/write/")
def write():
    return result(request.get_data(), request.content_type)
"""
    )
    candidate = tmp_path / "candidate"
    shutil.copytree(project, candidate)
    (candidate / "crud_config/serving_settings.py").write_text('MARKER = "candidate"\n')
    (candidate / "target_app.py").write_text(
        app + '\nfrom crud_config.serving_settings import MARKER\nassert MARKER == "candidate"\n'
    )
    supplied = [
        {"id": "nested", "method": "POST", "path": "/write/", "body": {"records": [{"code": "a"}]}},
        {
            "id": "upload",
            "method": "POST",
            "path": "/write/",
            "multipart": {
                "boundary": "Sample-73",
                "files": [{"field": "blob", "filename": "a.bin", "content_b64": "YQ=="}],
            },
        },
    ]
    probes = [
        p
        for p in edge_probes_from_scan(
            {"routes": [{"method": "POST", "path": "/write/"}]}, supplied
        )
        if p.get("probe_kind")
    ]
    report = replay(
        project,
        [*supplied, *probes],
        candidate_root=candidate,
        settings_module="crud_config.settings",
        python=Path(sys.executable),
        target=target,
    )
    rows = {r["id"]: r for r in report["scenarios"]}
    assert rows["nested"]["match"] and rows["upload"]["match"]
    assert rows["edge:upload-binary:upload"]["match"]
    assert not rows["edge:upload-boundary:upload"]["body_match"]
    assert not rows["edge:duplicate-record-0:nested"]["status_match"]
    assert not report["ok"]


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


def test_edge_probes_reuse_only_matching_request_context() -> None:
    scan = {
        "routes": [
            {"method": "GET", "path": "/items/{identifier}/"},
            {"method": "GET", "path": "/unrelated/"},
        ]
    }
    scenarios = [
        {"id": "unauthenticated", "method": "GET", "path": "/items/7/", "headers": {}},
        {
            "id": "success",
            "method": "GET",
            "path": "/items/7/?page=2",
            "headers": {"authorization": "fixture-token", "x-tenant": "a"},
            "expected_source_status": 200,
            "capture_headers": ["etag"],
            "setup": [{"method": "POST", "path": "/items/", "body": {"name": "item"}}],
        },
    ]
    original = copy.deepcopy(scenarios)
    probes = edge_probes_from_scan(scan, scenarios)
    selected = {p["method"]: p for p in probes if p["path"] == "/items/7/"}
    assert {"HEAD", "OPTIONS", "TRACE"} <= selected.keys()
    for probe in selected.values():
        assert probe["headers"] == {"authorization": "fixture-token", "x-tenant": "a"}
        assert probe["setup"] == scenarios[1]["setup"]
        assert probe["context_from"] == "success"
        assert "etag" in probe["capture_headers"]
        assert "body" not in probe and "expected_source_status" not in probe
    assert all(not p["headers"] for p in probes if p["generated_from"] == "/unrelated/")
    selected["HEAD"]["headers"].clear()
    assert selected["OPTIONS"]["headers"] and scenarios == original


def test_expected_source_status_is_validated_instead_of_silently_ignored(tmp_path: Path) -> None:
    path = tmp_path / "scenarios.json"
    scenario = {"id": "read", "method": "GET", "path": "/items/1/"}
    path.write_text(json.dumps([{**scenario, "expected_source_status": 200}]))
    assert load_scenarios(path)[0]["expected_source_status"] == 200
    for value in (True, None, "200", 99, 600):
        path.write_text(json.dumps([{**scenario, "expected_source_status": value}]))
        with pytest.raises(ReplayError, match="expected_source_status"):
            load_scenarios(path)
    path.write_text(
        json.dumps([{**scenario, "setup": [{**scenario, "expected_source_status": 201}]}])
    )
    with pytest.raises(ReplayError, match="top-level"):
        load_scenarios(path)


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
        "scenarios": [{"id": "list", "match": False, "native": {"compliant": True}}],
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
    assert response.artifacts == (response.data["report_path"],)
    assert "scenarios" not in response.data
    assert json.loads(Path(response.data["report_path"]).read_text()) == report


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


def test_replay_preserves_json_types_and_absent_body(tmp_path: Path) -> None:
    assert body_difference({"value": True}, {"value": 1}) is not None
    assert body_difference([False], [0]) is not None
    assert body_difference(1, 1.0) is None
    path = tmp_path / "bodies.json"
    path.write_text(
        json.dumps(
            [
                {"id": "absent", "path": "/"},
                {"id": "empty", "path": "/", "body": {}},
                {"id": "null", "path": "/", "body": None},
            ]
        )
    )
    absent, empty, null = load_scenarios(path)
    assert "body" not in absent
    assert empty["body"] == {}
    assert null["body"] is None and "body" in null


def test_compact_report_keeps_full_artifact_and_limits_failures(tmp_path: Path) -> None:
    reports = [
        {"id": f"failure-{n}", "match": False, "native": {"compliant": True}} for n in range(25)
    ]
    full = {
        "schema": "sanka-verify-replay/v1",
        "ok": False,
        "summary": {"scenarios": 25, "mismatched": 25},
        "scenarios": reports,
        "summary_lines": ["0/25"] + [item["id"] for item in reports],
    }
    compact = replay_module.save_report(full, tmp_path)
    assert len(compact["failures"]) == 20
    assert compact["omitted_failures"] == 5
    assert "scenarios" not in compact
    assert json.loads(Path(compact["report_path"]).read_text()) == full


def test_replay_cleans_temporary_database_on_failure(tmp_path: Path) -> None:
    (tmp_path / "target_app.py").touch()
    temp = tmp_path / "temporary"
    temp.mkdir()
    with (
        patch.object(replay_module.tempfile, "mkdtemp", return_value=str(temp)),
        patch.object(replay_module, "_run_side", side_effect=ReplayError("failed")),
        pytest.raises(ReplayError, match="failed"),
    ):
        replay(tmp_path, [{"id": "one", "method": "GET", "path": "/"}], settings_module="settings")
    assert not temp.exists()


def test_matching_errors_expose_coverage_warnings(tmp_path: Path) -> None:
    (tmp_path / "target_app.py").touch()
    reports = [
        {
            "id": str(status),
            "match": True,
            "status_match": True,
            "body_match": True,
            "headers_match": True,
            "database_match": True,
            "media_match": True,
            "native": {"compliant": True},
            "source": {"status": status},
            **({"generated_from": "/private/"} if status == 401 else {}),
        }
        for status in (404, 401)
    ]
    with (
        patch.object(replay_module, "_run_side", return_value={}),
        patch.object(replay_module, "_replay_one", side_effect=reports),
    ):
        report = replay(tmp_path, [{}, {}], settings_module="settings")
    assert report["ok"]  # Error-only parity is valid; it is not successful-path coverage.
    assert report["summary"]["source_statuses"] == {"401": 1, "404": 1}
    compact = replay_module.save_report(report, tmp_path / "artifacts")
    assert compact["coverage_issues"] == [
        {"code": "missing_success_coverage", "scenario_ids": ["404", "401"]},
        {"code": "authentication_coverage", "scenario_ids": ["401"]},
    ]
    assert len(compact["warnings"]) == 2
    assert "--seed" in compact["warnings"][0]
    assert "authenticated" in compact["warnings"][1]
    assert json.loads(Path(compact["report_path"]).read_text())["warnings"] == compact["warnings"]


@pytest.mark.parametrize("successful", [False, True])
@pytest.mark.parametrize("different_context", [None, "headers", "setup", "body", "multipart"])
def test_explicit_auth_rejection_requires_success_coverage(
    tmp_path: Path, successful: bool, different_context: str | None
) -> None:
    (tmp_path / "target_app.py").touch()
    reports = [
        {
            "id": "expected",
            "path": "/private/",
            "method": "OPTIONS",
            "expected_source_status": 403,
            "source": {"status": 403},
        },
        {
            "id": "probe",
            "path": "/private/",
            "method": "OPTIONS",
            "generated_from": "/private/",
            "source": {"status": 403},
        },
    ]
    if successful:
        reports.append(
            {"id": "success", "path": "/private/", "method": "GET", "source": {"status": 200}}
        )
    for item in reports:
        item.update(
            match=True,
            status_match=True,
            body_match=True,
            headers_match=True,
            database_match=True,
            media_match=True,
            native={"compliant": True},
        )
    scenarios = [{"method": r["method"], "path": r["path"]} for r in reports]
    if different_context:
        scenarios[1][different_context] = "different"
    with (
        patch.object(replay_module, "_run_side", return_value={}),
        patch.object(replay_module, "_replay_one", side_effect=reports),
    ):
        report = replay(tmp_path, scenarios, settings_module="settings")
    accepted = successful and different_context is None
    assert bool(report["warnings"]) is not accepted
    assert bool(report["coverage_issues"]) is not accepted


def test_source_expectation_failure_is_reported_as_coverage(tmp_path: Path) -> None:
    (tmp_path / "target_app.py").touch()
    result = {
        "id": "seeded-success",
        "method": "GET",
        "path": "/private/",
        "expected_source_status": 200,
        "source": {"status": 404},
        "match": False,
        "source_expectation_match": False,
        "status_match": True,
        "body_match": True,
        "headers_match": True,
        "database_match": True,
        "media_match": True,
        "native": {"compliant": True},
    }
    with (
        patch.object(replay_module, "_run_side", return_value={}),
        patch.object(replay_module, "_replay_one", return_value=result),
    ):
        report = replay(tmp_path, [{}], settings_module="settings")
    assert not report["ok"]
    assert report["summary"]["source_expectation_mismatches"] == 1
    assert report["summary"]["status_mismatches"] == 0
    assert report["coverage_issues"][0] == {
        "code": "source_expectation_mismatch",
        "scenario_ids": ["seeded-success"],
    }


@pytest.mark.parametrize("success", [True, False])
def test_generated_auth_context_uses_observed_source_success(tmp_path: Path, success: bool) -> None:
    (tmp_path / "target_app.py").touch()
    supplied = [
        {
            "id": "invalid",
            "method": "GET",
            "path": "/private/",
            "headers": {"Authorization": "invalid"},
        },
        {
            "id": "valid",
            "method": "GET",
            "path": "/private/",
            "headers": {"Authorization": "valid"},
        },
    ]
    probes = edge_probes_from_scan({"routes": [{"method": "GET", "path": "/private/"}]}, supplied)
    assert probes[0]["context_from"] == "invalid"
    observed = []

    def one(scenario, **kwargs):
        observed.append(dict(scenario))
        status = (
            200 if success and scenario.get("headers", {}).get("Authorization") == "valid" else 401
        )
        return {
            **scenario,
            "source": {"status": status},
            "match": True,
            "status_match": True,
            "body_match": True,
            "headers_match": True,
            "database_match": True,
            "media_match": True,
            "native": {"compliant": True},
        }

    with (
        patch.object(replay_module, "_run_side", return_value={}),
        patch.object(replay_module, "_replay_one", side_effect=one),
    ):
        result = replay(tmp_path, supplied + probes, settings_module="settings")
    assert observed[:2] == supplied
    assert probes[0]["context_from"] == "invalid"  # caller artifacts remain immutable
    assert observed[2]["context_from"] == ("valid" if success else "invalid")
    assert bool(result["coverage_issues"]) is not success


@pytest.mark.parametrize("success", [True, False])
def test_contract_probes_preserve_negative_context_and_require_baseline_success(
    tmp_path: Path, success: bool
) -> None:
    (tmp_path / "target_app.py").touch()
    supplied = [
        {
            "id": "create",
            "method": "POST",
            "path": "/items/",
            "body": {"name": "sample"},
            "expected_source_status": 201,
            "headers": {
                "Authorization": "Token valid",
                "X-CSRFToken": "valid",
                "Cookie": "sessionid=valid",
            },
        }
    ]
    scan = {
        "routes": [
            None,
            {
                "method": "POST",
                "path": "/items/",
                "serializer": "ItemSerializer",
                "authentication": ["TokenAuthentication", "SessionAuthentication"],
            },
        ],
        "serializer_details": [
            {
                "name": "ItemSerializer",
                "fields": [{"name": "id", "read_only": True}, {"name": "name", "read_only": False}],
            }
        ],
    }
    probes = [p for p in edge_probes_from_scan(scan, supplied) if p.get("probe_kind")]
    assert [p["probe_kind"] for p in probes] == [
        "read-only",
        "credential-rejection",
        "csrf-rejection",
    ]
    assert probes[0]["body"] == {"name": "sample", "id": {"sanka_read_only_probe": True}}
    assert "expected_source_status" not in probes[0]
    assert probes[1]["headers"]["authorization"] == "Token"
    assert "x-csrftoken" not in probes[2]["headers"]
    assert supplied[0]["body"] == {"name": "sample"}
    observed = []

    def one(scenario, **kwargs):
        observed.append(dict(scenario))
        status = (
            201
            if success
            and scenario.get("probe_kind") not in {"credential-rejection", "csrf-rejection"}
            else 403
        )
        return {
            **scenario,
            "source": {"status": status},
            "match": True,
            "status_match": True,
            "body_match": True,
            "headers_match": True,
            "database_match": True,
            "media_match": True,
            "native": {"compliant": True},
        }

    with (
        patch.object(replay_module, "_run_side", return_value={}),
        patch.object(replay_module, "_replay_one", side_effect=one),
    ):
        result = replay(tmp_path, supplied + probes, settings_module="settings")
    assert observed[2]["headers"]["authorization"] == "Token"
    assert "x-csrftoken" not in observed[3]["headers"]
    assert bool(result["coverage_issues"]) is not success
    assert all("baseline_source_status" not in p for p in probes)


def test_contract_probes_are_bounded_and_do_not_invent_contracts() -> None:
    scan = {
        "routes": [
            {"method": "POST", "path": f"/items/{i}/", "authentication": ["TokenAuthentication"]}
            for i in range(20)
        ]
    }
    scenarios = [
        {
            "id": str(i),
            "method": "POST",
            "path": f"/items/{i}/",
            "headers": {"Authorization": "Token valid"},
            "body": {"id": 1},
        }
        for i in range(20)
    ]
    probes = [p for p in edge_probes_from_scan(scan, scenarios) if p.get("probe_kind")]
    assert len(probes) == 12
    assert all(p["probe_kind"] == "credential-rejection" for p in probes)
    assert edge_probes_from_scan({"routes": [None], "serializer_details": [None]}, scenarios) == []

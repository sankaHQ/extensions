# SPDX-License-Identifier: Apache-2.0
"""Generate a subset, then extend it, and exercise the actual router."""

import dataclasses
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sanka_extension_python_to_golang.adapter import handle
from test_python_to_golang import request


@pytest.mark.skipif(os.getenv("SANKA_GO_TESTS") != "1", reason="requires Go toolchain")
@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_partial_then_cumulative_go_http(
    tmp_path: Path, target: str, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SANKA_GO_SOURCE_PYTHON", sys.executable)
    (tmp_path / "app.py").write_text("""from flask import Flask, jsonify
app = Flask(__name__)
@app.get("/one")
def one():
    return jsonify({"value": 1})
@app.get("/two")
def two():
    return jsonify({"value": 2})
""")
    base = request(tmp_path, target=target)
    for selected in (["GET /one"], ["GET /one", "GET /two"]):
        req = dataclasses.replace(
            base, configuration=base.configuration | {"selected_endpoints": selected}
        )
        planned = handle(req)
        assert planned.outcome == "success", planned.error
        assert planned.data["endpoint_scope"]["retained_ids"] == (
            [] if len(selected) == 1 else ["GET /one"]
        )
        applied = handle(
            dataclasses.replace(
                req,
                command="apply",
                reviewed_plan_hash="reviewed",
                configuration=req.configuration
                | {"extension_plan_hash": planned.data["plan_hash"]},
            )
        )
        assert applied.outcome == "success", applied.error
        output = Path(str(applied.data["output"]))
        dispatch = (
            "res, err := app.Test(req); if err != nil { t.Fatal(err) }; status := res.StatusCode"
            if target == "fiber"
            else "res := httptest.NewRecorder(); app.ServeHTTP(res, req); status := res.Code"
        )
        (output / "selection_test.go").write_text(
            'package backend\nimport("net/http/httptest"; "testing")\n'
            "func TestSelection(t *testing.T) { app := NewApp(); "
            'for path, want := range map[string]int{"/one":200, "/two":'
            + str(404 if len(selected) == 1 else 200)
            + '} { req := httptest.NewRequest("GET", path, nil); '
            + dispatch
            + '; if status != want { t.Fatalf("%s: got %d want %d",path,status,want) } } }'
        )
        for stage in ("test", "verify"):
            report = handle(dataclasses.replace(req, command=stage))
            assert report.outcome == "success", report.error
            assert report.data["ok"]
        result = subprocess.run(
            ["go", "test", "-mod=readonly", "-p=2", "-run", "^TestSelection$", "."],
            cwd=output,
            env=os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"},
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        if len(selected) == 1:
            source = tmp_path / "app.py"
            original = source.read_text()
            source.write_text(original.replace('"value": 2', '"value": 3'))
            rejected = handle(dataclasses.replace(req, command="verify"))
            assert rejected.outcome == "error"
            source.write_text(original)

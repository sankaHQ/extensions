# SPDX-License-Identifier: Apache-2.0
"""Real framework clients and native Go adapters; no listening test servers."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sanka_extension_python_to_golang.adapter import handle
from sanka_extension_python_to_golang.capture import SOURCES, TARGETS, capture, configuration

from sanka_extensions.code import ExtensionRequest

PAYLOAD = {
    "message": 'hello "world" 日本語',
    "items": [1, True, None, 9223372036854775809],
    "nested": {"ok": False},
}


def source(framework: str) -> str:
    body = repr(PAYLOAD)
    if framework == "flask":
        return f"""from flask import Flask, jsonify
app = Flask(__name__)
@app.get("/health")
def health():
    return jsonify({body})
"""
    if framework == "fastapi":
        return f"""from fastapi import FastAPI
app = FastAPI()
@app.get("/health")
async def health():
    return {body}
"""
    return f"""from django.urls import path
from rest_framework.decorators import (
    api_view, authentication_classes, permission_classes, renderer_classes
)
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
@renderer_classes([JSONRenderer])
def health(request):
    return Response({body})
urlpatterns = [path("health", health)]
"""


def request(root: Path, framework: str = "flask", target: str = "fiber") -> ExtensionRequest:
    return ExtensionRequest(
        "test",
        "plan",
        str(root),
        str(root / ".sanka" / "go"),
        "sanka/python-to-golang",
        "0.1.0a1",
        "0" * 64,
        {},
        {"source_framework": framework, "target_framework": target},
        (),
        None,
    )


def apply(root: Path, framework: str, target: str) -> Path:
    req = request(root, framework, target)
    planned = handle(req)
    assert planned.outcome == "success", planned.error
    assert handle(req).data == planned.data
    result = handle(
        dataclasses.replace(
            req,
            command="apply",
            reviewed_plan_hash="runtime-reviewed-plan",
            configuration=req.configuration | {"extension_plan_hash": planned.data["plan_hash"]},
        )
    )
    assert result.outcome == "success", result.error
    return Path(str(result.data["output"]))


@pytest.mark.parametrize("framework", SOURCES)
def test_review_and_fail_closed(tmp_path: Path, framework: str) -> None:
    (tmp_path / "app.py").write_text(source(framework))
    req = request(tmp_path, framework)
    plan = handle(req)
    assert plan.outcome == "success", plan.error
    assert plan.data["files"]
    assert handle(dataclasses.replace(req, command="apply")).outcome == "error"
    old_hash = plan.data["plan_hash"]
    (tmp_path / "app.py").write_text(source(framework) + "\nSECRET = 'changed'\n")
    blocked = handle(
        dataclasses.replace(
            req,
            command="apply",
            reviewed_plan_hash="runtime-reviewed-plan",
            configuration=req.configuration | {"extension_plan_hash": old_hash},
        )
    )
    assert blocked.outcome == "error"
    assert not (tmp_path / ".sanka/go/golang").exists()
    (tmp_path / "app.py").write_text(source(framework))
    output = apply(tmp_path, framework, "fiber")
    assert (output / "go.sum").is_file()
    assert (
        handle(
            dataclasses.replace(
                req,
                command="apply",
                reviewed_plan_hash="runtime-reviewed-plan",
                configuration=req.configuration | {"extension_plan_hash": old_hash},
            )
        ).outcome
        == "error"
    )


@pytest.mark.parametrize("addition", ["import os", "def helper():\n    return 1", "app = None"])
def test_unknown_behavior_blocks(tmp_path: Path, addition: str) -> None:
    (tmp_path / "app.py").write_text(source("flask") + "\n" + addition)
    plan = handle(request(tmp_path))
    assert plan.outcome == "success"
    assert plan.data["files"] == {}


def test_profiles_and_source_boundaries(tmp_path: Path) -> None:
    assert configuration({"source_framework": "flask"})["target_framework"] == "fiber"
    for config in (
        {"source_framework": "django"},
        {"source_framework": "flask", "database_layer": "pgx"},
        {"source_framework": "flask", "target_framework": "unknown"},
        {"source_framework": "flask", "source_file": "../app.py"},
    ):
        with pytest.raises(ValueError):
            configuration(config)
    (tmp_path / "app.py").write_text(source("flask"))
    (tmp_path / "models.py").write_text("raise RuntimeError('must not execute')")
    assert capture(tmp_path, configuration({"source_framework": "flask"}))["gaps"]
    (tmp_path / "models.py").unlink()
    (tmp_path / "link.py").symlink_to(tmp_path / "app.py")
    assert handle(request(tmp_path)).outcome == "error"


@pytest.mark.skipif(os.getenv("SANKA_GO_TESTS") != "1", reason="set SANKA_GO_TESTS=1 for Go matrix")
@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
def test_source_go_parity(tmp_path: Path, framework: str, target: str) -> None:
    (tmp_path / "app.py").write_text(source(framework))
    probe = (
        """import json
from django.conf import settings
settings.configure(SECRET_KEY="fixture", ROOT_URLCONF="app", ALLOWED_HOSTS=["testserver"],
 REST_FRAMEWORK={"UNAUTHENTICATED_USER": None}, INSTALLED_APPS=[])
import django
django.setup()
from django.test import Client
response = Client().get("/health")
print(json.dumps({"status": response.status_code, "body": response.json()}))
"""
        if framework == "drf"
        else (
            """import json
from app import app
from fastapi.testclient import TestClient
response = TestClient(app).get("/health")
print(json.dumps({"status": response.status_code, "body": response.json()}))
"""
            if framework == "fastapi"
            else """import json
from app import app
response = app.test_client().get("/health")
print(json.dumps({"status": response.status_code, "body": response.json}))
"""
        )
    )
    observed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
    )
    expected = json.loads(observed.stdout)
    assert expected == {"status": 200, "body": PAYLOAD}
    output = apply(tmp_path, framework, target)
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    tested = handle(dataclasses.replace(request(tmp_path, framework, target), command="test"))
    assert tested.outcome == "success", tested.error
    verified = handle(dataclasses.replace(request(tmp_path, framework, target), command="verify"))
    assert verified.outcome == "success", verified.error
    assert verified.data["source"] == [
        {
            "path": "/health",
            "status": expected["status"],
            "body": expected["body"],
            "media_type": "application/json",
        }
    ]
    assert verified.data["candidate"] == verified.data["source"]
    assert {path.name: path.read_bytes() for path in output.iterdir()} == before


def test_plan_tampering_and_review_attestation(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(source("flask"))
    req = request(tmp_path)
    plan = handle(req)
    approved = dataclasses.replace(
        req,
        command="apply",
        reviewed_plan_hash="runtime-review",
        configuration=req.configuration | {"extension_plan_hash": plan.data["plan_hash"]},
    )
    assert handle(dataclasses.replace(approved, reviewed_plan_hash=None)).outcome == "error"
    stored = tmp_path / ".sanka/go/plan.json"
    stored.write_text("{}")
    assert handle(approved).outcome == "error"
    assert not (tmp_path / ".sanka/go/golang").exists()
    handle(req)
    assert (
        handle(
            dataclasses.replace(
                approved, configuration=approved.configuration | {"target_framework": "chi"}
            )
        ).outcome
        == "error"
    )


def test_subprocess_protocol(tmp_path: Path) -> None:
    from sanka_extensions.code import encode_request

    (tmp_path / "app.py").write_text(source("fastapi"))
    result = subprocess.run(
        [sys.executable, "-m", "sanka_extension_python_to_golang"],
        input=json.dumps(encode_request(request(tmp_path, "fastapi"))),
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["outcome"] == "success"
    assert response["data"]["files"]["go.sum"]
    approved = dataclasses.replace(
        request(tmp_path, "fastapi"),
        command="apply",
        reviewed_plan_hash="runtime-review",
        configuration={
            "source_framework": "fastapi",
            "extension_plan_hash": response["data"]["plan_hash"],
        },
    )
    result = subprocess.run(
        [sys.executable, "-m", "sanka_extension_python_to_golang"],
        input=json.dumps(encode_request(approved)),
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    output = Path(json.loads(result.stdout)["data"]["output"])
    assert (output / "app.go").is_file()


def test_plan_is_independent_of_checkout_location(tmp_path: Path) -> None:
    plans = []
    for name in ("first", "second"):
        root = tmp_path / name
        root.mkdir()
        (root / "app.py").write_text(source("flask"))
        plans.append(handle(request(root)).data)
    assert plans[0] == plans[1]


@pytest.mark.skipif(os.getenv("SANKA_GO_TESTS") != "1", reason="set SANKA_GO_TESTS=1 for Go replay")
def test_replay_detects_candidate_changes(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(source("flask"))
    output = apply(tmp_path, "flask", "fiber")
    app = output / "app.go"
    # Python equality treats True == 1; JSON contracts must preserve the type.
    app.write_text(app.read_text().replace("[1,true,null", "[1,1,null"))
    req = dataclasses.replace(request(tmp_path), command="verify")
    response = handle(req)
    assert response.outcome == "error"
    assert response.error.code == "SANKA_EXTENSION_PARITY_FAILED"
    report = json.loads((tmp_path / ".sanka/go/verify.json").read_text())
    assert report["ok"] is False
    assert report["candidate_digest"]
    assert json.dumps(report["source"]) != json.dumps(report["candidate"])
    lock = output / "go.mod"
    lock.write_text(lock.read_text() + "\n// changed\n")
    response = handle(req)
    assert response.outcome == "error"
    assert "go.mod differs" in response.error.message
    assert not (tmp_path / ".sanka/go/verify.json").exists()


def test_replay_requires_current_applied_plan(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(source("flask"))
    req = dataclasses.replace(request(tmp_path), command="verify")
    assert handle(req).outcome == "error"
    output = apply(tmp_path, "flask", "fiber")
    extra = output / "extra.go"
    extra.symlink_to(tmp_path / "app.py")
    assert "regular files" in handle(req).error.message
    extra.unlink()
    (tmp_path / "app.py").write_text(source("flask") + "\n# changed\n")
    assert "differs from the applied plan" in handle(req).error.message

# SPDX-License-Identifier: Apache-2.0
"""Exercise the real isolated protocol and compare generated native HTTP behavior."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sanka_extension_sdk import ExtensionRequest, encode_request


def project(root: Path) -> None:
    (root / "manage.py").write_text(
        'import os; os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")'
    )
    (root / "settings.py").write_text(
        'SECRET_KEY="test"\nINSTALLED_APPS=[]\nMIDDLEWARE=[]\n'
        'ROOT_URLCONF="urls"\nALLOWED_HOSTS=["testserver"]\n'
        'REST_FRAMEWORK={"UNAUTHENTICATED_USER": None}\n'
    )
    (root / "urls.py").write_text("""from django.urls import path
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

class Quote(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    renderer_classes = [JSONRenderer]
    def get(self, request, quantity):
        label = request.query_params.get("label", "quote")
        return Response({"label": label, "total": quantity * 7}, headers={"X-Quote": "calculated"})
    def post(self, request, quantity):
        return Response(request.data)

class Secret(APIView):
    permission_classes = [IsAuthenticated]
    authentication_classes = []
    renderer_classes = [JSONRenderer]
    def get(self, request):
        return Response({"secret": True})

urlpatterns = [path("quotes/<int:quantity>/", Quote.as_view()), path("secret/", Secret.as_view())]
""")


def call(root: Path, command: str, config=None, reviewed=None):
    request = ExtensionRequest(
        "test",
        command,
        str(root),
        str(root / ".sanka"),
        "sanka/drf-to-flask",
        "0.1.0a1",
        "0" * 64,
        {},
        {"settings_module": "settings", **(config or {})},
        (),
        reviewed,
    )
    result = subprocess.run(
        [sys.executable, "-m", "sanka_extension_drf_to_flask"],
        input=json.dumps(encode_request(request)),
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(result.stdout)
    assert len(result.stdout.splitlines()) == 1, result.stdout
    return payload


def test_native_lifecycle_and_explicit_gaps(tmp_path: Path) -> None:
    project(tmp_path)
    scan = call(tmp_path, "scan")
    assert scan["outcome"] == "success", scan
    plan = call(tmp_path, "plan")["data"]
    assert plan["native_routes"] == 1
    assert plan["needs_adaptation_routes"] >= 2
    assert call(tmp_path, "plan")["data"]["plan_hash"] == plan["plan_hash"]
    applied = call(tmp_path, "apply", {"extension_plan_hash": plan["plan_hash"]}, "core-reviewed")
    assert applied["outcome"] == "success", applied
    output = Path(applied["data"]["output"])
    probe = """import json, sys
from flask import Flask
import target_app
assert isinstance(target_app.app, Flask)
assert not any(m == 'rest_framework' or m.startswith('rest_framework.') for m in sys.modules)
client = target_app.app.test_client()
r = client.get('/quotes/4/?label=ignored&label=bulk')
assert r.status_code == 200 and r.json == {'label': 'bulk', 'total': 28}
assert r.headers['X-Quote'] == 'calculated'
assert client.get('/secret/').status_code == 501
assert client.post('/quotes/4/', json={'value': 3}).status_code == 501
print(json.dumps({'status': r.status_code, 'body': r.json}))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=output,
        env=os.environ | {"PYTHONPATH": str(tmp_path)},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    source = """import os, django, json
os.environ['DJANGO_SETTINGS_MODULE']='settings'
django.setup()
from django.test import Client
r=Client().get('/quotes/4/?label=ignored&label=bulk')
print(json.dumps({'status':r.status_code,'body':r.json()}))
"""
    original = subprocess.run(
        [sys.executable, "-c", source], cwd=tmp_path, text=True, capture_output=True, check=True
    )
    assert json.loads(original.stdout) == json.loads(result.stdout)
    assert (
        call(tmp_path, "apply", {"extension_plan_hash": plan["plan_hash"]}, "core-reviewed")[
            "outcome"
        ]
        == "error"
    )


@pytest.mark.parametrize("mutation", ["source", "hash", "output", "symlink", "readiness"])
def test_apply_rejects_stale_or_unsafe_plan(tmp_path: Path, mutation: str) -> None:
    project(tmp_path)
    assert call(tmp_path, "scan")["outcome"] == "success"
    plan = call(tmp_path, "plan")["data"]
    config = {"extension_plan_hash": plan["plan_hash"]}
    if mutation == "source":
        with (tmp_path / "urls.py").open("a") as handle:
            handle.write("\n# changed after review\n")
    elif mutation == "hash":
        config["extension_plan_hash"] = "sha256:stale"
    elif mutation == "output":
        config["output"] = str(tmp_path.parent / "outside")
    elif mutation == "readiness":
        config["min_readiness"] = 100
    else:
        (tmp_path / "linked").symlink_to(tmp_path.parent, target_is_directory=True)
        config["bench_candidate"] = "linked/escape"
    assert call(tmp_path, "apply", config, "core-reviewed")["outcome"] == "error"
    assert not (tmp_path / ".sanka/output/flask").exists()

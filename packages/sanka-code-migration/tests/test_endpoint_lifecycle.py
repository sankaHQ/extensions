# SPDX-License-Identifier: Apache-2.0
"""Real protocol and generated HTTP boundary for cumulative endpoint selection."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sanka_extensions.code import ExtensionRequest, encode_request


@pytest.mark.parametrize("target", ["flask", "fastapi"])
def test_partial_then_cumulative_drf_http(tmp_path: Path, target: str):
    (tmp_path / "manage.py").write_text(
        "import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'settings')"
    )
    (tmp_path / "settings.py").write_text(
        "SECRET_KEY='test'\nINSTALLED_APPS=[]\nMIDDLEWARE=[]\n"
        "ROOT_URLCONF='urls'\nALLOWED_HOSTS=['testserver']\n"
        "REST_FRAMEWORK={'UNAUTHENTICATED_USER': None}\n"
    )
    (tmp_path / "urls.py").write_text("""from django.urls import path
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
class One(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    renderer_classes = [JSONRenderer]
    def get(self, request):
        return Response({"value": 1})
    def post(self, request):
        return Response({"value": 2})
urlpatterns = [path("one/", One.as_view())]
""")
    fixtures = Path(__file__).resolve().parents[2] / "sanka-extension-drf-to-fastapi" / "tests"
    if target == "fastapi":
        shutil.copytree(fixtures / "fixtures" / "drf_crud_project", tmp_path, dirs_exist_ok=True)
    route = "/api/gadgets/" if target == "fastapi" else "/one/"
    output = tmp_path / ".sanka" / "output" / target
    config = {
        "settings_module": "crud_config.settings" if target == "fastapi" else "settings",
        "output": str(output),
        "strategy": "native",
        "generation": "minimal",
        "package_manager": "uv",
    }

    def call(command, extra=None, reviewed=None):
        request = ExtensionRequest(
            "scope",
            command,
            str(tmp_path),
            str(tmp_path / ".sanka"),
            f"sanka/drf-to-{target}",
            "0.1.0a1",
            "0" * 64,
            {},
            config | (extra or {}),
            (),
            reviewed,
        )
        result = subprocess.run(
            [sys.executable, "-m", f"sanka_extension_drf_to_{target}"],
            input=json.dumps(encode_request(request)),
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.stdout, result.stderr
        return json.loads(result.stdout)

    assert call("scan")["outcome"] == "success"
    inventory = call("plan")
    assert inventory["outcome"] == "success", inventory
    ids = inventory["data"]["endpoint_scope"]["effective_ids"]
    read = next(key for key in ids if key == "GET " + route)
    write = next(key for key in ids if key == "POST " + route)
    for selected in ([read], [read, write]):
        planned = call("plan", {"selected_endpoints": selected})
        assert planned["outcome"] == "success", planned
        data = planned["data"]
        assert data["endpoint_scope"]["retained_ids"] == ([] if len(selected) == 1 else [read])
        applied = call(
            "apply",
            {"selected_endpoints": selected, "extension_plan_hash": data["plan_hash"]},
            "reviewed",
        )
        assert applied["outcome"] == "success", applied
        if target == "fastapi":
            scenarios = [
                {"method": "GET", "path": route},
                {"method": "POST", "path": route, "body": {"name": "New", "quantity": 2}},
            ]
            probe = subprocess.run(
                [
                    sys.executable,
                    str(fixtures / "native_parity_probe.py"),
                    "--mode",
                    "native",
                    "--project",
                    str(tmp_path),
                    "--output",
                    str(output),
                    "--database",
                    str(tmp_path / "probe.sqlite3"),
                    "--scenarios",
                    json.dumps(scenarios),
                ],
                capture_output=True,
                text=True,
            )
            assert probe.returncode == 0, probe.stderr
            statuses = [
                row["status"] for row in json.loads(probe.stdout.splitlines()[-1])["results"]
            ]
            assert statuses == [200, 405 if len(selected) == 1 else 201]
        else:
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from target_app import app; client=app.test_client(); "
                    "assert client.get('/one/').status_code == 200; "
                    + "assert client.post('/one/').status_code == "
                    + str(405 if len(selected) == 1 else 200)
                    + "; assert ('POST' in client.get('/one/').headers['Allow']) == "
                    + str(len(selected) == 2),
                ],
                cwd=output,
                env={**os.environ, "PYTHONPATH": os.pathsep.join((str(output), str(tmp_path)))},
                capture_output=True,
                text=True,
            )
            assert probe.returncode == 0, probe.stderr
        note = output / "notes.txt"
        if len(selected) == 1:
            note.write_text("keep")
        else:
            assert note.read_text() == "keep"
    (output / ("target_app.py" if target == "flask" else "app.py")).write_text("# user repair")
    assert call("plan", {"selected_endpoints": [read, write]})["outcome"] == "error"

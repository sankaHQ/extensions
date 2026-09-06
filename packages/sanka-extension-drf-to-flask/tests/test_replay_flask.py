# SPDX-License-Identifier: Apache-2.0
"""Real protocol replay, isolated fixtures, and deliberately broken Flask candidates."""

import base64
import json
from pathlib import Path

from test_lifecycle import call, project


def test_flask_replay_and_mutation_controls(tmp_path: Path) -> None:
    project(tmp_path)
    with (tmp_path / "settings.py").open("a") as handle:
        handle.write(
            'import os\nDATABASES={"default":{"ENGINE":"django.db.backends.sqlite3",'
            '"NAME":os.environ.get("SANKA_TEST_DB", "unused.sqlite3")}}\n'
        )
    (tmp_path / "urls.py").write_text("""from django.urls import path
from django.db import connection
from rest_framework.views import APIView
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
class Echo(APIView):
    authentication_classes = []
    renderer_classes = [JSONRenderer]
    def post(self, request):
        value = request.data
        with connection.cursor() as cursor:
            cursor.execute("UPDATE counter SET n=n+1")
        return Response({"value": value, "flag": True}, headers={"X-Check": "yes"})
urlpatterns = [path("echo/", Echo.as_view())]
""")
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    code = """import os
os.environ["DJANGO_SETTINGS_MODULE"] = "settings"
import django
django.setup()
from django.db import connection
from flask import Flask, request, jsonify
app = Flask(__name__)
@app.post("/echo/")
def echo():
    value = request.get_json() if request.get_data() else {}
    with connection.cursor() as cursor:
        cursor.execute("UPDATE counter SET n=n+1")
    return jsonify({"value": value, "flag": True}), 200, {"X-Check": "yes"}
"""
    (candidate / "target_app.py").write_text(code)
    seed = tmp_path / "seed.py"
    seed.write_text(
        "from django.db import connection\nwith connection.cursor() as c:\n"
        '    c.execute("CREATE TABLE counter (n INTEGER)")\n'
        '    c.execute("INSERT INTO counter VALUES (0)")\n'
    )
    scenarios = [
        {"id": "absent", "method": "POST", "path": "/echo/", "capture_headers": ["X-Check"]},
        {"id": "empty", "method": "POST", "path": "/echo/", "body": {}},
        {"id": "null", "method": "POST", "path": "/echo/", "body": None},
        {"id": "true", "method": "POST", "path": "/echo/", "body": True},
    ]
    (tmp_path / "scenarios.json").write_text(json.dumps(scenarios))
    config = {"scenarios": "scenarios.json", "candidate": "candidate", "seed": "seed.py"}
    result = call(tmp_path, "verify", config)
    assert result["outcome"] == "success", result
    assert result["data"]["summary"]["matched"] == 4
    assert result["data"]["summary"]["source_statuses"] == {"200": 4}
    assert result["data"]["warnings"] == []
    assert result["data"]["failures"] == []
    assert "scenarios" not in result["data"]
    full = json.loads(Path(result["data"]["report_path"]).read_text())
    assert len(full["scenarios"]) == 4
    assert not (tmp_path / "unused.sqlite3").exists()
    assert not list(candidate.glob("*.sqlite3"))
    # Parity alone must not pass when the caller's intended source status was not reached.
    (tmp_path / "scenarios.json").write_text(
        json.dumps([{**scenarios[0], "expected_source_status": 201}])
    )
    invalid_coverage = call(tmp_path, "verify", config)
    assert invalid_coverage["outcome"] == "error", invalid_coverage
    assert invalid_coverage["data"]["summary"]["source_expectation_mismatches"] == 1
    assert "expected 201" in invalid_coverage["data"]["failures"][0]["message"]
    (tmp_path / "scenarios.json").write_text(json.dumps(scenarios))
    for before, after, dimension in [
        ('"flag": True', '"flag": 1', "body_mismatches"),
        ('"X-Check": "yes"', '"X-Check": "no"', "header_mismatches"),
        ("n=n+1", "n=n+2", "database_mismatches"),
        ("}), 200,", "}), 201,", "status_mismatches"),
    ]:
        assert before in code
        (candidate / "target_app.py").write_text(code.replace(before, after))
        broken = call(tmp_path, "verify", config)
        assert broken["outcome"] == "error", broken
        assert broken["error"]["code"] == "SANKA_EXTENSION_REPLAY_MISMATCH"
        assert broken["data"]["summary"][dimension] > 0, broken
        assert broken["data"]["failures"]
    (candidate / "target_app.py").write_text(code + "\nimport rest_framework\n")
    assert call(tmp_path, "verify", config)["data"]["summary"]["non_native"] == 4
    # Deliberately malformed input reaches the source and candidate as identical bytes.
    (tmp_path / "scenarios.json").write_text(
        json.dumps(
            [
                {
                    "id": "malformed",
                    "method": "POST",
                    "path": "/echo/",
                    "headers": {"Content-Type": "application/json"},
                    "body_base64": base64.b64encode(b"{").decode(),
                }
            ]
        )
    )
    (candidate / "target_app.py").write_text(code)
    malformed = call(tmp_path, "verify", config)
    assert malformed["data"]["summary"]["body_mismatches"] == 1
    assert malformed["data"]["summary"]["source_statuses"] == {"400": 1}
    assert "--seed" in malformed["data"]["warnings"][0]
    # A settings module ignoring the isolation variable must fail before touching its DB.
    with (tmp_path / "settings.py").open("a") as handle:
        handle.write('DATABASES["default"]["NAME"]="do-not-touch.sqlite3"\n')
    invalid = call(tmp_path, "verify", config)
    assert invalid["error"]["code"] == "SANKA_EXTENSION_REPLAY_INVALID", invalid
    assert not (tmp_path / "do-not-touch.sqlite3").exists()

# SPDX-License-Identifier: Apache-2.0
"""Independent upload fixture; generated handlers must match the source parser."""

import json
import os
import subprocess
import sys
from pathlib import Path

from test_lifecycle import call, project


def test_generated_forms_preserve_source_uploads(tmp_path: Path) -> None:
    project(tmp_path)
    (tmp_path / "urls.py").write_text("""from django.urls import path
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
class Upload(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    renderer_classes = [JSONRenderer]
    parser_classes = [FormParser, MultiPartParser]
    def post(self, request):
        data = request.data
        blob = data.get("blob")
        return Response({"label": data.get("label"), "labels": data.getlist("label"),
                         "hex": blob.read().hex() if blob else None,
                         "name": blob.name if blob else None})
urlpatterns = [path("uploads/", Upload.as_view())]
""")
    assert call(tmp_path, "scan")["outcome"] == "success"
    plan = call(tmp_path, "plan")["data"]
    assert plan["needs_adaptation_routes"] == 0
    applied = call(tmp_path, "apply", {"extension_plan_hash": plan["plan_hash"]}, "reviewed")
    assert applied["outcome"] == "success", applied
    assert "sanka_form.parse_form" in applied["data"]["repair_guidance"]
    output = Path(applied["data"]["output"])
    probe = r"""import json, os, sys
mode = sys.argv[1]
if mode == "target":
    from target_app import app
    client = app.test_client()
else:
    os.environ["DJANGO_SETTINGS_MODULE"] = "settings"
    import django
    django.setup()
    from django.test import Client
    client = Client()
rows=[]
payloads = [b"ordinary\r\n", b"\x00\xffbytes", b"start--CargoBoundary-stop",
            b"start--CargoBoundary--end", b""]
for payload in payloads:
    raw = (b'--CargoBoundary\r\n'
           b'Content-Disposition: form-data; name="label"\r\n\r\none\r\n'
           b'--CargoBoundary\r\n'
           b'Content-Disposition: form-data; name="label"\r\n\r\ntwo\r\n'
           b'--CargoBoundary\r\n'
           b'Content-Disposition: form-data; name="blob"; filename="cargo.bin"\r\n'
           b'Content-Type: application/octet-stream\r\n\r\n'
           + payload + b'\r\n--CargoBoundary--\r\n')
    fn = client.post if mode == "target" else lambda path, **kw: client.generic("POST",path,**kw)
    response = fn("/uploads/", data=raw, content_type="multipart/form-data; boundary=CargoBoundary")
    rows.append([response.status_code,response.get_json() if mode == "target" else response.json()])
    response.close()
for raw, media in [(b"label=one&label=two", "application/x-www-form-urlencoded"),
                   (b"{}", "application/json")]:
    response = fn("/uploads/",data=raw,content_type=media)
    rows.append([response.status_code,response.get_json() if mode == "target" else response.json()])
    response.close()
if mode == "target":
    assert not any(m == "rest_framework" or m.startswith("rest_framework.") for m in sys.modules)
print(json.dumps(rows))
"""
    results = []
    for mode, directory in [("source", tmp_path), ("target", output)]:
        result = subprocess.run(
            [sys.executable, "-c", probe, mode],
            cwd=directory,
            env=os.environ | {"PYTHONPATH": str(tmp_path)},
            capture_output=True,
            text=True,
            check=True,
        )
        results.append(json.loads(result.stdout))
    assert results[0] == results[1]
    assert results[1][0][1]["hex"] == b"ordinary\r\n".hex()
    assert results[1][3][1]["hex"] == b"start".hex()

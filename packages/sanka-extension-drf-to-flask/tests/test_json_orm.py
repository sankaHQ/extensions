# SPDX-License-Identifier: Apache-2.0
"""Independent stockroom fixture: JSON parsing and ORM writes, no benchmark inputs."""

import json
import os
import subprocess
import sys
from pathlib import Path

from test_lifecycle import call, project


def test_json_orm_conversion_matches_source(tmp_path: Path) -> None:
    project(tmp_path)
    (tmp_path / "stock").mkdir()
    (tmp_path / "stock/__init__.py").touch()
    (tmp_path / "stock/models.py").write_text("""from django.db import models
class Item(models.Model):
    label = models.CharField(max_length=50)
    units = models.IntegerField(default=0)
    unit_cost = models.DecimalField(max_digits=7, decimal_places=2, default="1.20")
    received = models.DateField(default="2026-09-05")
""")
    with (tmp_path / "settings.py").open("a") as handle:
        handle.write(
            'INSTALLED_APPS=["stock"]\nDATABASES={"default":{"ENGINE":"django.db.backends.sqlite3","NAME":":memory:"}}\n'
        )
    (tmp_path / "urls.py").write_text("""from django.urls import path
from django.db import transaction
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.parsers import JSONParser
from rest_framework.response import Response
from stock.models import Item

class Stock(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    renderer_classes = [JSONRenderer]
    parser_classes = [JSONParser]
    def get(self, request):
        return Response(list(Item.objects.order_by("id").values()))
    def post(self, request):
        data = request.data
        if not isinstance(data, dict) or not data.get("label"):
            return Response({"label": ["Provide a label."]}, status=400)
        data["units"] = int(data.get("units", 0)) + 1
        with transaction.atomic():
            item = Item.objects.create(label=data["label"], units=request.data["units"])
        return Response(
            {"label": item.label, "units": item.units},
            status=201, headers={"X-Stock": "created"},
        )

urlpatterns = [path("stock/", Stock.as_view())]
""")
    assert call(tmp_path, "scan")["outcome"] == "success"
    plan = call(tmp_path, "plan")["data"]
    assert plan["native_routes"] == 2
    applied = call(tmp_path, "apply", {"extension_plan_hash": plan["plan_hash"]}, "reviewed")
    assert applied["outcome"] == "success", applied
    probe = """import os, sys, json
target = sys.argv[1] == 'target'
if target:
    import target_app
    client = target_app.app.test_client()
else:
    os.environ['DJANGO_SETTINGS_MODULE'] = 'settings'
    import django
    django.setup()
    from django.test import Client
    client = Client()
from django.db import connection
from stock.models import Item
with connection.schema_editor() as schema:
    schema.create_model(Item)
records = []
cases = [(body, "application/json") for body in [
    '{}', '{"label":"bolts","units":4}', '[]', '{', 'NaN', '',
]]
for body, media in cases + [("a=b", "text/plain")]:
    response = client.post('/stock/', data=body, content_type=media)
    records.append([
        response.status_code, response.get_json() if target else response.json(),
        response.headers.get('X-Stock'), list(Item.objects.values('label', 'units')),
    ])
response = client.get('/stock/')
records.append([response.status_code, response.get_json() if target else response.json()])
if target:
    assert not any(m == 'rest_framework' or m.startswith('rest_framework.') for m in sys.modules)
print(json.dumps(records))
"""
    records = []
    for mode, directory in [("source", tmp_path), ("target", Path(applied["data"]["output"]))]:
        result = subprocess.run(
            [sys.executable, "-c", probe, mode],
            cwd=directory,
            env=os.environ | {"PYTHONPATH": str(tmp_path)},
            text=True,
            capture_output=True,
            check=True,
        )
        records.append(json.loads(result.stdout))
    assert records[0] == records[1]
    with (tmp_path / "stock/models.py").open("a") as handle:
        handle.write("\nimport rest_framework\n")
    scan = call(tmp_path, "scan")["data"]
    assert all(route["classification"] == "needs_adaptation" for route in scan["routes"])

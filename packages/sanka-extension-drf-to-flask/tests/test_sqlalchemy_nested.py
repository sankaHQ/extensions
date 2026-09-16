# SPDX-License-Identifier: Apache-2.0
"""Captured nested writes execute atomically without a source framework."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from sanka_code_migration.drf.model import FrameworkScan
from test_model_viewsets import project

from sanka_extension_drf_to_flask.database import render_database
from sanka_extension_drf_to_flask.planning import _service_operations
from sanka_extension_drf_to_flask.sqlalchemy import qualify_routes, render_sqlalchemy


def test_nested_source_and_standalone_target(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    project(source)
    settings = source / "settings.py"
    settings.write_text(
        settings.read_text().replace(
            '["django.contrib.auth","django.contrib.contenttypes","catalog"]', '["catalog"]'
        )
        + '\nUSE_TZ=True\nTIME_ZONE="UTC"\nDEFAULT_AUTO_FIELD="django.db.models.AutoField"\n'
    )
    settings.write_text(
        settings.read_text() + "\nREST_FRAMEWORK.update({"
        "'DEFAULT_AUTHENTICATION_CLASSES': [],"
        "'DEFAULT_RENDERER_CLASSES': ['rest_framework.renderers.JSONRenderer'],"
        "'DEFAULT_PARSER_CLASSES': ['rest_framework.parsers.JSONParser']})\n"
    )
    serializers = source / "catalog/serializers.py"
    serializers.write_text(
        serializers.read_text().replace(
            "if sum(p.quantity for p in bundle.parts.all()) > 12:",
            "total = sum(p.quantity for p in bundle.parts.all())\n            if total > 12:",
        )
    )
    cases = [
        ["POST", "/api/bundles/", {"code": "first", "parts": [{"quantity": 2, "cost": "3.40"}]}],
        ["GET", "/api/bundles/1/", None],
        ["GET", "/api/bundles/", None],
        [
            "POST",
            "/api/bundles/",
            {"code": "rollback", "parts": [{"quantity": 13, "cost": "1.00"}]},
        ],
        [
            "POST",
            "/api/bundles/",
            {"code": "invalid", "parts": [{"quantity": 0, "cost": "1.234"}, {}]},
        ],
        ["POST", "/api/bundles/", {"code": "missing"}],
        ["POST", "/api/bundles/", {"code": "null", "parts": None}],
        ["POST", "/api/bundles/", {"code": "nonlist", "parts": {}}],
        ["POST", "/api/bundles/", {"code": "childnull", "parts": [None, "x"]}],
        ["POST", "/api/bundles/", {"code": "empty", "parts": []}],
        ["PATCH", "/api/bundles/1/", {"state": "ready", "parts": [{"quantity": 9}]}],
        ["PUT", "/api/bundles/1/", {"code": "updated", "parts": [{"quantity": 4, "cost": "2.00"}]}],
        ["PATCH", "/api/bundles/1/", {}],
        ["DELETE", "/api/bundles/1/", None],
    ]
    (source / "cases.json").write_text(json.dumps(cases))
    capture = """
import json, sys
from pathlib import Path
from sanka_code_migration.drf.scan import scan_django
from sanka_code_migration.drf.models import capture_schema
from sanka_extension_drf_to_flask.sqlalchemy import capture_sqlalchemy_overrides
scan = scan_django(sys.argv[1], settings_module='settings')
from catalog.models import Bundle, Part
from django.db import connection
from rest_framework.test import APIClient
with connection.schema_editor() as editor:
    editor.create_model(Bundle)
    editor.create_model(Part)
client = APIClient()
results = []
for method, path, data in json.loads(Path(sys.argv[1], 'cases.json').read_text()):
    response = client.generic(method, path, json.dumps(data) if data is not None else '',
                              content_type='application/json')
    body = json.loads(response.content) if response.content else None
    results.append([response.status_code, body,
        list(Bundle.objects.order_by('id').values()), list(Part.objects.order_by('id').values())])
assert results[0][0] == 201 and results[3][0] == 400
assert len(results[3][2]) == 1 and len(results[3][3]) == 1
print(json.dumps({'scan':scan.to_dict(), 'schema':capture_schema([Bundle,Part]),
    'overrides':capture_sqlalchemy_overrides(scan), 'results':results}, default=str))
"""
    env = dict(os.environ)
    result = subprocess.run(
        [sys.executable, "-c", capture, str(source)],
        cwd=source,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    facts = json.loads(result.stdout)
    assert not facts["schema"]["gaps"], facts["schema"]["gaps"]
    scan = FrameworkScan.from_dict(facts["scan"])
    assert "create:catalog.serializers.BundleSerializer" in {
        operation.name
        for operation in _service_operations(scan, facts["schema"])
        if operation.coordinates_multiple_writes
    }
    rows = qualify_routes(scan, facts["overrides"])
    assert all(r["native"] for r in rows), sorted({g for r in rows for g in r["gaps"]})
    files = {
        **render_database(facts["schema"]),
        **render_sqlalchemy(scan, facts["schema"], overrides=facts["overrides"]),
    }
    target = tmp_path / "target"
    target.mkdir()
    for name, text in files.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (target / "cases.json").write_text(json.dumps(cases))
    (target / "results.json").write_text(json.dumps(facts["results"]))
    env["SANKA_DATABASE_URL"] = "sqlite:///" + str(target / "target.sqlite3")
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    probe = """
import importlib.abc, json, sys
from pathlib import Path
class NoSource(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in {'django','rest_framework','catalog','sanka_code_migration',
                                  'sanka_extension_drf_to_flask'}:
            raise ImportError('source forbidden: ' + name)
sys.meta_path.insert(0, NoSource())
from target_app import create_app
from models import TABLES
import sqlalchemy as sa
app = create_app({'TESTING': True})
client = app.test_client()
cases = json.loads(Path('cases.json').read_text())
expected_results = json.loads(Path('results.json').read_text())
for (method, path, data), expected in zip(cases, expected_results, strict=True):
    response = client.open(path, method=method,
        data=json.dumps(data) if data is not None else '',
        content_type='application/json', base_url='http://testserver')
    with app.extensions['sanka_engine'].connect() as connection:
        snapshots = []
        for name in ['catalog_bundle','catalog_part']:
            query = sa.select(TABLES[name]).order_by(TABLES[name].c.id)
            snapshots.append(list(map(dict, connection.execute(query).mappings())))
    observed = [response.status_code, response.json if response.data else None, *snapshots]
    observed = json.loads(json.dumps(observed, default=str))
    assert observed == expected, (method, path, data, observed, expected)
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr

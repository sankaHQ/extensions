# SPDX-License-Identifier: Apache-2.0
"""Recognized conditional responses preserve successful writes and error behavior."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from sanka_code_migration.drf.model import FrameworkScan

from sanka_extension_drf_to_flask.database import render_database
from sanka_extension_drf_to_flask.sqlalchemy import qualify_routes, render_sqlalchemy


@pytest.mark.parametrize("timezone", ["UTC", "Asia/Tokyo"])
def test_conditional_record_responses_and_database_effects(
    tmp_path: Path, timezone: str, contract_databases
) -> None:
    fixture = (
        Path(__file__).resolve().parents[2]
        / "sanka-extension-drf-to-fastapi/tests/fixtures/drf_records_project"
    )
    source = tmp_path / "source"
    shutil.copytree(fixture, source)
    settings = source / "precision_project/settings.py"
    settings.write_text(
        settings.read_text() + "\nTIME_ZONE='UTC'\nREST_FRAMEWORK['DEFAULT_PARSER_CLASSES']="
        "['rest_framework.parsers.JSONParser']\n"
        "import os,json\n"
        "DATABASES={'default':json.loads(os.environ['SANKA_SOURCE_TEST_DATABASE'])}\n"
    )
    body = {
        "label": "First",
        "category": "ops",
        "amount": "12.30",
        "posted_at": "2026-09-16T12:00:00Z",
    }
    cases = [
        ["POST", "/api/records/", body, None],
        ["GET", "/api/records/1/", None, None],
        ["GET", "/api/records/1/", None, "*"],
        ["GET", "/api/records/1/", None, "saved-etag"],
        ["GET", "/api/records/1/", None, "weak-etag"],
        ["PATCH", "/api/records/1/", {"amount": "bad"}, "*"],
        ["GET", "/api/records/999/", None, "*"],
        ["PATCH", "/api/records/1/", {"amount": "45.60"}, "*"],
        ["GET", "/api/records/1/", None, None],
        ["PUT", "/api/records/1/", {**body, "label": "Changed"}, "saved-etag"],
        ["PATCH", "/api/records/1/", {}, "saved-etag"],
        ["OPTIONS", "/api/records/1/", None, "*"],
        ["DELETE", "/api/records/1/", None, "*"],
    ]
    (source / "cases.json").write_text(json.dumps(cases))
    capture = """
import json,sys
from pathlib import Path
from sanka_code_migration.drf.scan import scan_django
from sanka_code_migration.drf.models import capture_schema
from sanka_extension_drf_to_flask.sqlalchemy import capture_sqlalchemy_overrides
scan = scan_django(sys.argv[1],settings_module='precision_project.settings')
from records.models import Record
from django.db import connection
from rest_framework.test import APIClient
with connection.schema_editor() as editor: editor.create_model(Record)
client=APIClient();results=[];etag=''
for method,path,body,condition in json.loads(Path(sys.argv[1],'cases.json').read_text()):
    if condition=='saved-etag': condition=etag
    elif condition=='weak-etag': condition='W/'+etag
    headers = {'HTTP_IF_NONE_MATCH':condition} if condition else {}
    response=client.generic(method,path,json.dumps(body) if body is not None else '',
                            content_type='application/json',**headers)
    etag=response.headers.get('ETag') or etag
    body = json.loads(response.content) if response.content else None
    results.append([response.status_code, body,
        {k:response.headers.get(k) for k in ['ETag','Cache-Control','Vary','Allow']},
        list(Record.objects.order_by('id').values('id','label','category','amount'))])
assert results[0][0]==201 and results[7][0]==304
assert results[7][3][0]['amount']==__import__('decimal').Decimal('45.60')
print(json.dumps({'scan':scan.to_dict(),'schema':capture_schema([Record]),
 'overrides':capture_sqlalchemy_overrides(scan),'results':results},default=str))
"""
    source_database, target_url = contract_databases
    env = dict(os.environ)
    env["SANKA_SOURCE_TEST_DATABASE"] = json.dumps(source_database)
    env["TZ"] = timezone
    run = subprocess.run(
        [sys.executable, "-c", capture, str(source)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    facts = json.loads(run.stdout)
    scan = FrameworkScan.from_dict(facts["scan"])
    rows = qualify_routes(scan, facts["overrides"])
    assert all(r["native"] for r in rows), sorted({g for r in rows for g in r["gaps"]})
    original_view = scan.view_details[0]
    assert original_view.carryover is not None
    changed = {
        **original_view.carryover,
        "methods": [
            {
                **method,
                "source": method["source"].replace("private, max-age=0", "public, max-age=60"),
            }
            for method in original_view.carryover["methods"]
        ],
    }
    unknown = replace(scan, view_details=(replace(original_view, carryover=changed),))
    assert any("carryover" in r["gaps"] for r in qualify_routes(unknown, facts["overrides"]))
    files = {
        **render_database(facts["schema"]),
        **render_sqlalchemy(scan, facts["schema"], overrides=facts["overrides"]),
    }
    target = tmp_path / "target"
    target.mkdir()
    for name, text in files.items():
        p = target / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    (target / "cases.json").write_text(json.dumps(cases))
    (target / "results.json").write_text(json.dumps(facts["results"]))
    env["SANKA_DATABASE_URL"] = target_url
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
import importlib.abc,json,sys
from pathlib import Path
class NoSource(importlib.abc.MetaPathFinder):
    def find_spec(self,name,path=None,target=None):
        if name.split('.')[0] in {'django','rest_framework','records','sanka_code_migration',
                                  'sanka_extension_drf_to_flask'}:
            raise ImportError('source forbidden: '+name)
sys.meta_path.insert(0,NoSource())
from target_app import create_app
from models import TABLES
import sqlalchemy as sa
app=create_app({'TESTING':True});client=app.test_client();etag=''
cases=json.loads(Path('cases.json').read_text())
expected_results=json.loads(Path('results.json').read_text())
for (method,path,body,condition),expected in zip(cases,expected_results,strict=True):
    if condition=='saved-etag': condition=etag
    elif condition=='weak-etag': condition='W/'+etag
    headers={'If-None-Match':condition} if condition else {}
    r=client.open(path,method=method,data=json.dumps(body) if body is not None else '',
                  content_type='application/json',headers=headers,base_url='http://testserver')
    etag=r.headers.get('ETag') or etag
    table=TABLES['records_record']
    with app.extensions['sanka_engine'].connect() as connection:
        query=sa.select(table.c.id,table.c.label,table.c.category,table.c.amount).order_by(table.c.id)
        rows=list(map(dict,connection.execute(query).mappings()))
    observed=[r.status_code,r.json if r.data else None,
              {k:r.headers.get(k) for k in ['ETag','Cache-Control','Vary','Allow']},rows]
    observed=json.loads(json.dumps(observed,default=str))
    assert observed==expected,(method,path,condition,observed,expected)
app.extensions['sanka_engine'].dispose()
"""
    run = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stdout + run.stderr

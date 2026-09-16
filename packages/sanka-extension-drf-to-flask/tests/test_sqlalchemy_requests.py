# SPDX-License-Identifier: Apache-2.0
"""Request-size failures preserve the source envelope and leave database rows unchanged."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_lifecycle import call
from test_native_plan import native_project


@pytest.mark.parametrize("middleware", [False, True])
def test_request_body_limit_matches_source(tmp_path: Path, middleware: bool):
    source = tmp_path / "source"
    native_project(source)
    if middleware:
        with (source / "crud_config/settings.py").open("a") as handle:
            handle.write(
                "\nMIDDLEWARE=['django.middleware.security.SecurityMiddleware',"
                "'django.middleware.common.CommonMiddleware']\n"
            )
    config = {"settings_module": "crud_config.settings", "orm": "sqlalchemy"}
    assert call(source, "scan", config)["outcome"] == "success"
    plan = call(source, "plan", config)
    assert plan["outcome"] == "success", plan
    config["extension_plan_hash"] = plan["data"]["plan_hash"]
    applied = call(source, "apply", config, "reviewed")
    assert applied["outcome"] == "success", applied
    target = Path(applied["data"]["output"])
    payloads = []
    for size in (2_621_439, 2_621_440, 2_621_441):
        payload = json.dumps(
            {"name": "bounded", "quantity": 1, "padding": ""}, separators=(",", ":")
        )
        payload = payload[:-2] + "x" * (size - len(payload)) + payload[-2:]
        assert len(payload.encode()) == size
        payloads.append(payload)
    request_file = tmp_path / "requests.json"
    request_file.write_text(json.dumps(payloads))
    env = os.environ | {"SANKA_TEST_DB": str(tmp_path / "source.sqlite3")}
    source_probe = """
import os,sys,json,django
from pathlib import Path
sys.path.insert(0,sys.argv[1]);os.environ['DJANGO_SETTINGS_MODULE']='crud_config.settings'
django.setup()
from django.db import connection
from inventory.models import Gadget
from rest_framework.test import APIClient
with connection.schema_editor() as editor: editor.create_model(Gadget)
client=APIClient();observed=[]
for body in json.loads(Path(sys.argv[2]).read_text()):
    r=client.generic('POST','/api/gadgets/',body,content_type='application/json')
    observed.append([r.status_code,r.content.decode(),dict(r.headers),
                     list(Gadget.objects.order_by('id').values())])
assert [r[0] for r in observed]==[201,201,400],observed
assert observed[-1][3]==observed[-2][3]
print(json.dumps(observed))
"""
    result = subprocess.run(
        [sys.executable, "-c", source_probe, str(source), str(request_file)],
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    expected_file = tmp_path / "expected.json"
    expected_file.write_text(result.stdout)
    env["SANKA_DATABASE_URL"] = "sqlite:///" + str(tmp_path / "target.sqlite3")
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=target,
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    )
    target_probe = """
import importlib.abc,json,sys
from pathlib import Path
class NoSource(importlib.abc.MetaPathFinder):
    def find_spec(self,name,path=None,target=None):
        if name.split('.')[0] in {'django','rest_framework','inventory','sanka_code_migration',
                                  'sanka_extension_drf_to_flask'}:
            raise ImportError('source forbidden: '+name)
sys.meta_path.insert(0,NoSource())
from target_app import create_app
from models import TABLES
import sqlalchemy as sa
app=create_app({'TESTING':True});client=app.test_client();engine=app.extensions['sanka_engine']
for body,expected in zip(json.loads(Path(sys.argv[1]).read_text()),
                         json.loads(Path(sys.argv[2]).read_text()),strict=True):
    r=client.post('/api/gadgets/',data=body,content_type='application/json',base_url='http://testserver')
    with engine.connect() as conn:
        table=TABLES['inventory_gadget']
        rows=list(map(dict,conn.execute(sa.select(table).order_by(table.c.id)).mappings()))
    observed=[r.status_code,r.get_data(as_text=True),dict(r.headers),rows]
    assert observed==expected,(observed,expected)
engine.dispose()
"""
    result = subprocess.run(
        [sys.executable, "-c", target_probe, str(request_file), str(expected_file)],
        cwd=target,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr

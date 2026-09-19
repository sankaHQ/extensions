# SPDX-License-Identifier: Apache-2.0
"""Literal CRUD response contracts preserve real DRF HTTP and database behavior."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from sanka_code_migration.drf.model import FrameworkScan

from sanka_extension_drf_to_flask.database import render_database
from sanka_extension_drf_to_flask.sqlalchemy import qualify_routes, render_sqlalchemy


@pytest.mark.parametrize("partial_override", [False, True])
def test_literal_responses_match_source(tmp_path, contract_databases, partial_override):
    fixture = (
        Path(__file__).parents[2] / "sanka-extension-drf-to-fastapi/tests/fixtures/drf_auth_project"
    )
    source = tmp_path / "source"
    shutil.copytree(fixture, source)
    settings = source / "board_config/settings.py"
    settings.write_text(
        settings.read_text() + "\nimport json\n"
        "DATABASES={'default':json.loads(os.environ['SANKA_SOURCE_TEST_DATABASE'])}\n"
        "REST_FRAMEWORK.update({'DEFAULT_RENDERER_CLASSES':['rest_framework.renderers.JSONRenderer'],"
        "'DEFAULT_PARSER_CLASSES':['rest_framework.parsers.JSONParser']})\n"
    )
    urls = source / "board_config/urls.py"
    urls.write_text(urls.read_text().replace("DefaultRouter", "SimpleRouter"))
    views = source / "bulletins/views.py"
    overrides = """
    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        response['X-Stage'] = 'list'
        response.status_code = 203
        response.data = {'items': response.data,
                         'meta': {'ok': True, 'empty': None, 'offset': -1.5}}
        return response
    def create(self, request, *args, **kwargs):
        response = super().create(request, *args, **kwargs)
        response['X-Stage'] = 'create'
        response.status_code = 202
        response.data = {'created': response.data}
        return response
    def retrieve(self, request, *args, **kwargs):
        response = super().retrieve(request, *args, **kwargs)
        response['X-Stage'] = 'retrieve'
        response.data = [response.data, {'kind': 'record'}]
        return response
    def update(self, request, *args, **kwargs):
        response = super().update(request, *args, **kwargs)
        response['X-Stage'] = 'update'
        response.data = {'updated': response.data}
        return response
    def destroy(self, request, *args, **kwargs):
        response = super().destroy(request, *args, **kwargs)
        response['X-Stage'] = 'destroy'
        response.status_code = 200
        response.data = {'deleted': response.data}
        return response
"""
    if partial_override:
        overrides += """
    def partial_update(self, request, *args, **kwargs):
        response = super().partial_update(request, *args, **kwargs)
        response['X-Stage'] = 'patch'
        response.data = {'patched': response.data}
        return response
"""
    else:
        overrides = overrides.replace(
            "        response.status_code = 200\n"
            "        response.data = {'deleted': response.data}\n",
            "",
        )
    views.write_text(views.read_text() + overrides)
    capture = """
import json,sys
from sanka_code_migration.drf.scan import scan_django
from sanka_code_migration.drf.models import capture_schema
from sanka_extension_drf_to_flask.sqlalchemy import capture_sqlalchemy_overrides
scan=scan_django(sys.argv[1],settings_module='board_config.settings')
from django.db import connection
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.models import Permission,Group,User
from rest_framework.authtoken.models import Token
from bulletins.models import Bulletin
from rest_framework.test import APIClient
with connection.schema_editor() as editor:
    for model in (ContentType,Permission,Group,User,Token,Bulletin): editor.create_model(model)
user=User.objects.create(username='alice')
Token.objects.create(key='alice',user=user)
bob=User.objects.create(username='bob')
Token.objects.create(key='bob',user=bob)
cases=[
 ['GET','/api/bulletins/',None,''],
 ['POST','/api/bulletins/',{},'Token alice'],
 ['POST','/api/bulletins/',{'title':'first'},'Token alice'],
 ['GET','/api/bulletins/',None,'Token alice'],
 ['HEAD','/api/bulletins/',None,'Token alice'],
 ['GET','/api/bulletins/1/',None,'Token alice'],
 ['HEAD','/api/bulletins/1/',None,'Token alice'],
 ['PUT','/api/bulletins/1/',{'title':'second'},'Token alice'],
 ['PATCH','/api/bulletins/1/',{'title':'third'},'Token alice'],
 ['PATCH','/api/bulletins/1/',{'title':''},'Token alice'],
 ['PATCH','/api/bulletins/999/',{'title':'missing'},'Token alice'],
 ['OPTIONS','/api/bulletins/1/',None,'Token alice'],
 ['POST','/api/bulletins/1/',{},'Token alice'],
 ['PATCH','/api/bulletins/1/',{'title':'forbidden'},'Token bob'],
 ['DELETE','/api/bulletins/1/',None,'Token alice'],
 ['GET','/api/bulletins/1/',None,'Token alice'],
]
client=APIClient();results=[]
for method,path,body,header in cases:
    response=client.generic(method,path,json.dumps(body) if body is not None else '',
        content_type='application/json',HTTP_AUTHORIZATION=header)
    results.append([response.status_code,json.loads(response.content) if response.content else None,
        {k:response.headers.get(k) for k in ['Allow','X-Stage','Content-Type','WWW-Authenticate']},
        list(Bulletin.objects.order_by('id').values())])
deleted_status=200 if sys.argv[2]=='True' else 204
assert [r[0] for r in results]==[
    401,400,202,203,203,200,200,200,200,400,404,200,405,403,deleted_status,404]
assert results[8][3][0]['title']=='third' and results[9][3]==results[8][3]
assert results[13][3]==results[8][3]
assert results[14][1]==({'deleted':None} if deleted_status==200 else None)
assert results[14][3]==[]
print(json.dumps({'scan':scan.to_dict(),'schema':capture_schema([Bulletin,Token]),
 'overrides':capture_sqlalchemy_overrides(scan),'cases':cases,'results':results}))
"""
    source_database, target_url = contract_databases
    env = os.environ | {"SANKA_SOURCE_TEST_DATABASE": json.dumps(source_database)}
    run = subprocess.run(
        [sys.executable, "-c", capture, str(source), str(partial_override)],
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
    target = tmp_path / "target"
    target.mkdir()
    files = {
        **render_database(facts["schema"]),
        **render_sqlalchemy(scan, facts["schema"], overrides=facts["overrides"]),
    }
    for name, content in files.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (target / "reference.json").write_text(json.dumps(facts))
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
        if name.split('.')[0] in {'django','rest_framework','bulletins','sanka_code_migration',
                                  'sanka_extension_drf_to_flask'}:
            raise ImportError('source forbidden: '+name)
sys.meta_path.insert(0,NoSource())
from target_app import create_app
from models import TABLES
import sqlalchemy as sa
app=create_app({'TESTING':True});engine=app.extensions['sanka_engine'];client=app.test_client()
with engine.begin() as conn:
    for pk,name in [(1,'alice'),(2,'bob')]:
        conn.execute(sa.insert(TABLES['auth_user']).values(id=pk,username=name,password='',
            is_superuser=False,first_name='',last_name='',email='',is_staff=False,is_active=True,
            date_joined=__import__('datetime').datetime.now(__import__('datetime').UTC)))
        conn.execute(sa.insert(TABLES['authtoken_token']).values(key=name,user_id=pk,
            created=__import__('datetime').datetime.now(__import__('datetime').UTC)))
facts=json.loads(Path('reference.json').read_text())
for (method,path,body,header),expected in zip(facts['cases'],facts['results'],strict=True):
    r=client.open(path,method=method,data=json.dumps(body) if body is not None else '',
        content_type='application/json',headers={'Authorization':header},base_url='http://testserver')
    with engine.connect() as conn:
        table=TABLES['bulletins_bulletin']
        rows=list(map(dict,conn.execute(sa.select(table).order_by(table.c.id)).mappings()))
    observed=[r.status_code,r.json if r.data else None,
        {k:r.headers.get(k) for k in ['Allow','X-Stage','Content-Type','WWW-Authenticate']},rows]
    assert observed==expected,(method,path,observed,expected)
engine.dispose()
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


@pytest.mark.parametrize(
    "statement",
    [
        "response['Content-Type'] = 'text/plain'",
        "response['Content-Length'] = '0'",
        "response['Vary'] = 'Accept'",
        "response['Allow'] = 'GET'",
        "response['Set-Cookie'] = 'secret=value'",
        "response['Keep-Alive'] = 'timeout=5'",
        "response['Proxy-Authenticate'] = 'Basic'",
        "response['Proxy-Authorization'] = 'Basic'",
        "response['X-Test'] = request.headers.get('X-Test')",
        "response['X-Test'] = 'bad\\nheader'",
        "response.status_code = 400",
        "response.status_code = 204",
        "response.status_code = 205",
        "response.status_code = 202; response.status_code = 200",
        "response.data = {'result': request.data}",
        "response.data = {'result': str(response.data)}",
        "response.data = {'constant': True}",
        "request.data['changed'] = True",
        "response.data = {'result': response.data}; response.data = {'again': response.data}",
        "response = super().list(request, *args, **kwargs)",
    ],
)
def test_unknown_response_statements_remain_gaps(statement):
    from sanka_extension_drf_to_flask.sqlalchemy_carryover import capture_response_overrides

    method = (
        "def retrieve(self, request, *args, **kwargs):\n"
        "    response = super().retrieve(request, *args, **kwargs)\n"
        "    " + statement + "\n    return response\n"
    )
    assert (
        capture_response_overrides(
            {
                "methods": [{"name": "retrieve", "source": method}],
                "operations": ["retrieve"],
                "imports": [],
            }
        )
        is None
    )


def test_shadowed_super_is_rejected_during_source_capture(tmp_path):
    from test_native_plan import native_project

    native_project(tmp_path)
    views = tmp_path / "inventory/views.py"
    views.write_text(
        views.read_text()
        + """
    def retrieve(self, request, *args, **kwargs):
        response = super().retrieve(request, *args, **kwargs)
        response['X-Test'] = 'literal'
        return response
super = lambda: None
"""
    )
    script = """
import json,sys
from sanka_code_migration.drf.scan import scan_django
from sanka_extension_drf_to_flask.sqlalchemy import capture_sqlalchemy_overrides,qualify_routes
scan=scan_django(sys.argv[1],settings_module='crud_config.settings')
rows=qualify_routes(scan,capture_sqlalchemy_overrides(scan))
assert rows and all('shadowed-super' in row['gaps'] for row in rows),rows
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "signature, first, decorator",
    [
        ("self, request, *args, **kwargs", "response = super().list(request, *args, **kwargs)", ""),
        (
            "self, request, *args, **kwargs",
            "response = super().retrieve(request, args, kwargs)",
            "",
        ),
        (
            "self, request=None, *args, **kwargs",
            "response = super().retrieve(request, *args, **kwargs)",
            "",
        ),
        (
            "self, request, *args, **kwargs",
            "response = super().retrieve(request, *args, **kwargs)",
            "@staticmethod\n",
        ),
    ],
)
def test_nonstock_method_call_or_binding_remains_a_gap(signature, first, decorator):
    from sanka_extension_drf_to_flask.sqlalchemy_carryover import capture_response_overrides

    method = (
        decorator + "def retrieve(" + signature + "):\n    " + first + "\n    return response\n"
    )
    assert (
        capture_response_overrides(
            {
                "methods": [{"name": "retrieve", "source": method}],
                "operations": ["retrieve"],
                "imports": [],
            }
        )
        is None
    )


@pytest.mark.parametrize(
    "parent_method, child_method",
    [
        ("retrieve", "retrieve"),
        ("update", "update"),
        ("partial_update", "update"),
        ("update", "partial_update"),
        (None, "retrieve"),
    ],
)
def test_custom_parent_response_chain_is_not_silently_dropped(
    tmp_path, parent_method, child_method
):
    from test_native_plan import native_project

    native_project(tmp_path)
    views = tmp_path / "inventory/views.py"
    parent = "class Parent(ModelViewSet):\n    pass\n"
    if parent_method:
        parent = (
            "class Parent(ModelViewSet):\n"
            f"    def {parent_method}(self, request, *args, **kwargs):\n"
            f"        response = super().{parent_method}(request, *args, **kwargs)\n"
            "        response['X-Parent'] = 'preserved'\n        return response\n"
        )
    views.write_text(
        views.read_text().replace(
            "class GadgetViewSet(ModelViewSet):", parent + "\nclass GadgetViewSet(Parent):"
        )
        + f"\n    def {child_method}(self, request, *args, **kwargs):\n"
        f"        response = super().{child_method}(request, *args, **kwargs)\n"
        "        response['X-Child'] = 'preserved'\n        return response\n"
    )
    script = """
import json,sys
from sanka_code_migration.drf.scan import scan_django
from sanka_extension_drf_to_flask.sqlalchemy import capture_sqlalchemy_overrides,qualify_routes
scan=scan_django(sys.argv[1],settings_module='crud_config.settings')
from django.db import connection
from inventory.models import Gadget
from rest_framework.test import APIClient
with connection.schema_editor() as editor: editor.create_model(Gadget)
Gadget.objects.create(name='existing',quantity=1)
method='GET' if sys.argv[2]=='retrieve' else 'PATCH'
response=APIClient().generic(method,'/api/gadgets/1/',json.dumps({'quantity':5}),
                            content_type='application/json')
assert response.status_code==200
assert response.headers['X-Child']=='preserved'
if sys.argv[3]!='None': assert response.headers['X-Parent']=='preserved'
assert Gadget.objects.get(pk=1).quantity==(1 if method=='GET' else 5)
rows=qualify_routes(scan,capture_sqlalchemy_overrides(scan))
if sys.argv[3]=='None':
    assert rows and all(row['native'] for row in rows),rows
else:
    assert rows and all(not row['native'] for row in rows),rows
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), child_method, str(parent_method)],
        env=os.environ | {"SANKA_TEST_DB": str(tmp_path / "source.db")},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_borrowed_method_class_cell_is_not_treated_as_the_callback_class(tmp_path):
    from test_native_plan import native_project

    native_project(tmp_path)
    views = tmp_path / "inventory/views.py"
    views.write_text(
        views.read_text()
        + """
class Foreign(ModelViewSet):
    def retrieve(self, request, *args, **kwargs):
        response = super().retrieve(request, *args, **kwargs)
        response['X-Test'] = 'literal'
        return response
GadgetViewSet.retrieve = Foreign.retrieve
"""
    )
    script = """
import sys
from sanka_code_migration.drf.scan import scan_django
from sanka_extension_drf_to_flask.sqlalchemy import capture_sqlalchemy_overrides,qualify_routes
scan=scan_django(sys.argv[1],settings_module='crud_config.settings')
rows=qualify_routes(scan,capture_sqlalchemy_overrides(scan))
assert rows and all(not row['native'] for row in rows),rows
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr

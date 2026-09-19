# SPDX-License-Identifier: Apache-2.0
"""Real django-filter source and generated Flask filtering stay identical."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from sanka_code_migration.drf.model import FrameworkScan

from sanka_extension_drf_to_flask.database import render_database
from sanka_extension_drf_to_flask.sqlalchemy import qualify_routes, render_sqlalchemy


def test_stock_django_filters_match_source_without_source_imports(
    tmp_path, contract_databases
) -> None:
    source = tmp_path / "source"
    app = source / "catalog"
    app.mkdir(parents=True)
    (app / "__init__.py").write_text("")
    (source / "settings.py").write_text(
        """
import json, os
SECRET_KEY='test'; INSTALLED_APPS=['django_filters','rest_framework','catalog']
DATABASES={'default':json.loads(os.environ['SANKA_SOURCE_TEST_DATABASE'])}
ROOT_URLCONF='urls'; MIDDLEWARE=[]; ALLOWED_HOSTS=['testserver']
DEFAULT_AUTO_FIELD='django.db.models.AutoField'; USE_TZ=True
REST_FRAMEWORK={'DEFAULT_AUTHENTICATION_CLASSES': [], 'UNAUTHENTICATED_USER': None,
 'DEFAULT_FILTER_BACKENDS':['django_filters.rest_framework.DjangoFilterBackend'],
 'DEFAULT_RENDERER_CLASSES':['rest_framework.renderers.JSONRenderer'],
 'DEFAULT_PARSER_CLASSES':['rest_framework.parsers.JSONParser']}
"""
    )
    (app / "models.py").write_text(
        """
from django.db import models
class Item(models.Model):
    name=models.CharField(max_length=20)
    state=models.CharField(max_length=10,choices=[('open','Open'),('closed','Closed')])
    score=models.IntegerField(null=True)
    active=models.BooleanField(null=True)
    class Meta: ordering=['id']
"""
    )
    (app / "views.py").write_text(
        """
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import serializers, viewsets
from .models import Item
class ItemSerializer(serializers.ModelSerializer):
    class Meta: model=Item; fields=('id','name','state','score','active')
class Items(viewsets.ModelViewSet):
    queryset=Item.objects.all(); serializer_class=ItemSerializer
    filter_backends=(DjangoFilterBackend,)
    filterset_fields={'name':['exact','iexact','in'],'state':['exact','in'],
      'score':['exact','in','gt','gte','lt','lte','isnull'],
      'active':['exact','isnull']}
"""
    )
    (source / "urls.py").write_text(
        """
from rest_framework.routers import DefaultRouter
from catalog.views import Items
router=DefaultRouter(); router.register('items',Items); urlpatterns=router.urls
"""
    )
    cases = [
        ["GET", "/items/?name=", None],
        ["GET", "/items/?name=%20%20", None],
        ["GET", "/items/?name=a%00b", None],
        ["GET", "/items/?name__iexact=alpha", None],
        ["GET", "/items/?name__in=Alpha,Beta", None],
        ["GET", "/items/?state=open", None],
        ["GET", "/items/?state=bad", None],
        ["GET", "/items/?state__in=open,bad", None],
        ["GET", "/items/?score__gte=2", None],
        ["GET", "/items/?score=1.9", None],
        ["GET", "/items/?score=1e0", None],
        ["GET", "/items/?score__gte=1.9", None],
        ["GET", "/items/?score__lt=3.9", None],
        ["GET", "/items/?score__in=1.9,3.9", None],
        ["GET", "/items/?score=1e30", None],
        ["GET", "/items/?score__gt=-1e30", None],
        ["GET", "/items/?score__gte=1e30", None],
        ["GET", "/items/?score__lt=-1e30", None],
        ["GET", "/items/?score__lte=1e30", None],
        ["GET", "/items/?score=1&score=3", None],
        ["GET", "/items/?score=bad", None],
        ["GET", "/items/?score__in=1,bad", None],
        ["GET", "/items/?state=bad&score=bad", None],
        ["GET", "/items/?score__isnull=true", None],
        ["GET", "/items/?active=false", None],
        ["GET", "/items/?active=unknown", None],
        ["GET", "/items/1/?state=closed", None],
        ["GET", "/items/not-an-id/", None],
        ["GET", "/items/not-an-id/?score=bad", None],
        ["GET", "/items/999/?score=bad", None],
        [
            "PUT",
            "/items/1/?score=bad",
            {"name": "Changed", "state": "open", "score": 9, "active": True},
        ],
        ["PATCH", "/items/1/?score=bad", {"name": "Changed"}],
        ["DELETE", "/items/1/?score=bad", None],
    ]
    capture = r"""
import json,sys
from sanka_code_migration.drf.scan import scan_django
from sanka_code_migration.drf.models import capture_schema
from sanka_extension_drf_to_flask.sqlalchemy import capture_sqlalchemy_overrides
scan=scan_django(sys.argv[1],settings_module='settings')
from catalog.models import Item
from django.db import connection
from django.http import QueryDict
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.test import APIClient
with connection.schema_editor() as editor: editor.create_model(Item)
Item.objects.bulk_create([
 Item(id=1,name='Alpha',state='open',score=1,active=True),
 Item(id=2,name='Beta',state='closed',score=3,active=False),
 Item(id=3,name='Gamma',state='open',score=None,active=None)])
# django-filter's stock `in` lookup does not apply Django's scalar integer-range
# optimization. Preserve its database error instead of silently dropping operands.
from catalog.views import Items
if connection.vendor=='sqlite':
 filterset=DjangoFilterBackend().get_filterset_class(Items(),Item.objects.all())
 for raw in ('score__in=1,1e30','score__in=1e30'):
  value=filterset(QueryDict(raw),queryset=Item.objects.all())
  assert value.is_valid()
  try: list(value.qs)
  except OverflowError: pass
  else: raise AssertionError('SQLite source unexpectedly accepted out-of-range IN')
client=APIClient(); out=[]
for method,path,body in json.loads(sys.argv[2]):
 response=client.generic(method,path,json.dumps(body) if body is not None else '',
                         content_type='application/json')
 out.append([response.status_code,json.loads(response.content) if response.content else None])
print(json.dumps({'scan':scan.to_dict(),'schema':capture_schema([Item]),
 'overrides':capture_sqlalchemy_overrides(scan),'responses':out}))
"""
    source_database, target_url = contract_databases
    env = dict(os.environ)
    env["SANKA_SOURCE_TEST_DATABASE"] = json.dumps(source_database)
    run = subprocess.run(
        [sys.executable, "-c", capture, str(source), json.dumps(cases)],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    )
    facts = json.loads(run.stdout)
    by_path = {
        path: response
        for (_method, path, _body), response in zip(cases, facts["responses"], strict=True)
    }
    assert by_path["/items/?state=bad"] == [
        400,
        {"state": ["Select a valid choice. bad is not one of the available choices."]},
    ]
    assert by_path["/items/?score=bad"] == [400, {"score": ["Enter a number."]}]
    assert by_path["/items/?name=a%00b"] == [
        400,
        {"name": ["Null characters are not allowed."]},
    ]
    assert by_path["/items/?state=bad&score=bad"] == [
        400,
        {
            "state": ["Select a valid choice. bad is not one of the available choices."],
            "score": ["Enter a number."],
        },
    ]
    assert by_path["/items/1/?state=closed"][0] == 404
    assert by_path["/items/not-an-id/"][0] == 404
    assert by_path["/items/not-an-id/?score=bad"] == [
        400,
        {"score": ["Enter a number."]},
    ]
    assert by_path["/items/999/?score=bad"] == [400, {"score": ["Enter a number."]}]

    scan = FrameworkScan.from_dict(facts["scan"])
    assert all(row["native"] for row in qualify_routes(scan, facts["overrides"]))
    target = tmp_path / "target"
    target.mkdir()
    for name, content in {
        **render_database(facts["schema"]),
        **render_sqlalchemy(scan, facts["schema"], overrides=facts["overrides"]),
    }.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (target / "contract.json").write_text(
        json.dumps({"cases": cases, "responses": facts["responses"]})
    )
    env["SANKA_DATABASE_URL"] = target_url
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=target,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    probe = r"""
import importlib.abc,json,sys
from pathlib import Path
class NoSource(importlib.abc.MetaPathFinder):
 def find_spec(self,name,path=None,target=None):
  forbidden={'django','django_filters','rest_framework','catalog',
             'sanka_code_migration','sanka_extension_drf_to_flask'}
  if name.split('.')[0] in forbidden:
   raise ImportError('source forbidden: '+name)
sys.meta_path.insert(0,NoSource())
from target_app import create_app
from models import TABLES
app=create_app({'TESTING':True}); table=TABLES['catalog_item']
with app.extensions['sanka_engine'].begin() as connection:
 connection.execute(table.insert(),[
  {'id':1,'name':'Alpha','state':'open','score':1,'active':True},
  {'id':2,'name':'Beta','state':'closed','score':3,'active':False},
  {'id':3,'name':'Gamma','state':'open','score':None,'active':None}])
contract=json.loads(Path('contract.json').read_text()); client=app.test_client(); observed=[]
for method,path,body in contract['cases']:
 response=client.open(path,method=method,json=body,base_url='http://testserver')
 observed.append([response.status_code,response.json if response.data else None])
assert observed==contract['responses'],json.dumps(
 {'observed':observed,'expected':contract['responses']},indent=2)
with app.extensions['sanka_engine'].connect() as connection:
 query=table.select().with_only_columns(
  table.c.id,table.c.name,table.c.state,table.c.score,table.c.active).order_by(table.c.id)
 rows=connection.execute(query).fetchall()
 assert rows==[(1,'Alpha','open',1,True),(2,'Beta','closed',3,False),(3,'Gamma','open',None,None)]
app.extensions['sanka_engine'].dispose()
"""
    run = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=target,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stdout + run.stderr

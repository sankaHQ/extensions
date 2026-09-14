# SPDX-License-Identifier: Apache-2.0
"""DRF scalar parity and existing Django UUID storage, without a full migration."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_uuid_boolean_capture_and_runtime(tmp_path: Path) -> None:
    script = r"""
import asyncio, json, sys, types, uuid
from dataclasses import asdict
from pathlib import Path
sys.path[:] = json.loads(sys.argv[2])
from django.conf import settings
settings.configure(INSTALLED_APPS=['django.contrib.contenttypes','rest_framework'],
    DATABASES={'default': {'ENGINE':'django.db.backends.sqlite3','NAME':':memory:'}})
import django
django.setup()
from django.db import models
from rest_framework import fields, serializers
from rest_framework.exceptions import ValidationError
from sanka_extension_drf_to_fastapi.django_fastapi import _serializer_field_ir, _field_payload
from sanka_extension_drf_to_fastapi.native_async import _RUNTIME, _render_models, _render_store
from sanka_extension_drf_to_fastapi.model import SerializerFieldIR
class List(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    purchased = models.BooleanField()
    class Meta: app_label='uuid_probe'
class Item(models.Model):
    parent = models.ForeignKey(List, on_delete=models.CASCADE)
    class Meta: app_label='uuid_probe'
class S(serializers.ModelSerializer):
    class Meta:
        model=List
        fields=['id','purchased']
folder=Path(sys.argv[1])
(folder/'sanka-manifest.json').write_text('{}')
sys.modules['sanka_store']=types.ModuleType('sanka_store')
runtime={'__file__':str(folder/'sanka_native.py')}
exec(compile(_RUNTIME,runtime['__file__'],'exec'),runtime)
for name, field in S().fields.items():
    ir=_serializer_field_ir(name,field,List)
    assert ir.supported,(name,ir)
    assert SerializerFieldIR.from_dict(asdict(ir))==ir
    if name=='id': assert ir.kind=='uuid' and ir.uuid_default
related=_serializer_field_ir('parent',serializers.PrimaryKeyRelatedField(read_only=True),Item)
assert related.kind=='related_uuid' and related.attname=='parent_id' and related.supported
values=[True,False,1,0,1.0,0.0,2,'TRUE','false','On','OFF','yes','n','1','0','null','',[],{},'bad',None]
for nullable in (False,True):
    field=fields.BooleanField(allow_null=nullable)
    spec=_field_payload(_serializer_field_ir('purchased',field,List))
    for value in values:
        result, errors=runtime['_validate_scalar_fields']([spec],{'purchased':value},partial=False)
        try: expected=field.run_validation(value)
        except ValidationError as exc:
            assert errors=={'purchased':[str(x) for x in exc.detail]},(value,errors,exc.detail)
        else:
            assert not errors and result['purchased']==expected,(value,result,expected)
field=fields.UUIDField()
spec=_field_payload(_serializer_field_ir('id',field,List))
values=[uuid.UUID(int=7), str(uuid.UUID(int=7)), uuid.UUID(int=7).hex,
    7, True, -1, 2**128, 'bad', [], {}, 1.5]
for value in values:
    result,errors=runtime['_clean_uuid'](spec,value)
    try: expected=field.run_validation(value)
    except ValidationError as exc:
        assert errors==[str(x) for x in exc.detail],(value,errors,exc.detail)
    else:
        assert not errors and result==expected
        assert runtime['_represent'](spec,result)==field.to_representation(expected)
assert not _serializer_field_ir('id',fields.UUIDField(format='hex'),List).supported
class CustomUUID(fields.UUIDField): pass
assert not _serializer_field_ir('id',CustomUUID(),List).supported
class CustomBoolean(fields.BooleanField): pass
assert not _serializer_field_ir('purchased',CustomBoolean(),List).supported
class CustomDefault(models.Model):
    id=models.UUIDField(primary_key=True,default=lambda:uuid.uuid4())
    class Meta:app_label='uuid_probe'
assert not _serializer_field_ir('id',fields.UUIDField(),CustomDefault).supported
class RequiredUUID(models.Model):
    id=models.UUIDField(primary_key=True)
    class Meta:app_label='uuid_probe'
assert _serializer_field_ir('id',fields.UUIDField(),RequiredUUID).supported
from starlette.requests import Request
request=Request({'type':'http','path_params':{'pk':'parent','item_pk':'child'}})
assert runtime['_lookup_value']({'lookup':'pk','lookup_url_kwarg':'item_pk'},request)=='child'
assert runtime['_lookup_value']({'lookup':'pk'},request)=='parent'
from sanka_extension_drf_to_fastapi.model import FrameworkScan, ViewIR, SerializerIR
captured=_serializer_field_ir('id',S().fields['id'],List)
serializer=SerializerIR(name='Probe',model='Probe',db_table='probe',pk_attname='id',
    model_module='probe',model_class='Probe',object_name='Probe',fields=(captured,))
scan=FrameworkScan(schema_version=8,source='.',language='python',framework='drf',
    python_version='3',django_version='5',drf_version='3',settings_module='probe',
    root_urlconf='probe',routes=(),serializer_details=(serializer,),
    view_details=(ViewIR(name='Probe'),))
payload=scan.hash_payload()
assert 'uuid_default' not in payload['serializer_details'][0]['fields'][0]
assert 'default_on_create_only' not in payload['serializer_details'][0]['fields'][0]
assert 'lookup_url_kwarg' not in payload['view_details'][0]

from django.core.validators import MaxValueValidator
assert not _serializer_field_ir('purchased',fields.BooleanField(
    validators=[MaxValueValidator(0)]),List).supported
class Defaults(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.UUID(int=8))
    purchased=models.BooleanField(default=False)
    class Meta:app_label='uuid_probe'
class DefaultSerializer(serializers.ModelSerializer):
    class Meta:model=Defaults;fields=['id','purchased']
default_specs=[_field_payload(_serializer_field_ir(n,f,Defaults))
    for n,f in DefaultSerializer().fields.items()]
assert all(s['default_on_create_only'] for s in default_specs)
assert runtime['_validate_scalar_fields'](default_specs,{},partial=False)==({},{})
from sanka_extension_drf_to_fastapi.django_fastapi import _build_serializer_ir
class MissingKey(serializers.ModelSerializer):
    class Meta:model=List;fields=['purchased']
assert not _build_serializer_ir(
    MissingKey,List,name='MissingKey',ordering=(),analyze_writes=False).supported


resource={'db_table':'uuid_list','model_class':'UUIDList','pk_attname':'id','lookup':'pk',
    'fields':[_field_payload(_serializer_field_ir(n,f,List)) for n,f in S().fields.items()]}
for engine in ('tortoise','sqlalchemy','psycopg'):
    scope={'__file__':str(folder/'sanka_store.py')}
    exec(compile(_render_store(engine),scope['__file__'],'exec'),scope)
    data={'purchased':False}
    fresh=scope['_creation_values'](resource,data)
    assert isinstance(fresh['id'],uuid.UUID) and data=={'purchased':False}
    assert fresh['id']!=scope['_creation_values'](resource,data)['id']
    assert scope['_creation_values']({'fields':default_specs},{})=={
        'id':uuid.UUID(int=8),'purchased':False}

    expected=uuid.UUID(int=123)
    assert scope['_coerce_lookup'](resource,str(expected))==(expected,'')
    assert scope['_coerce_lookup'](resource,'invalid')==(None,'invalid')
manifest={'resources':[resource]}
from sqlalchemy.dialects import postgresql,sqlite
scope={};exec(_render_models('sqlalchemy',manifest),scope)
columns=scope['UUIDList'].__table__.columns
assert str(columns['id'].type.compile(dialect=sqlite.dialect()))=='CHAR(32)'
assert str(columns['id'].type.compile(dialect=postgresql.dialect()))=='UUID'
assert str(columns['purchased'].type.compile(dialect=postgresql.dialect()))=='BOOLEAN'
# The generated field must address an existing Django SQLite UUID column (32 hex digits).
module=types.ModuleType('uuid_generated_models')
exec(_render_models('tortoise',manifest),module.__dict__)
sys.modules[module.__name__]=module
from tortoise import Tortoise
async def check_storage():
    await Tortoise.init(db_url='sqlite://:memory:',modules={'models':[module.__name__]})
    try:
        conn=Tortoise.get_connection('default')
        await conn.execute_script(
            'CREATE TABLE uuid_list (id char(32) PRIMARY KEY, purchased bool NOT NULL);')
        id=uuid.UUID(int=42)
        await conn.execute_query('INSERT INTO uuid_list VALUES (?,?)',[id.hex,0])
        obj=await module.UUIDList.get(id=id)
        assert obj.id==id and obj.purchased is False
        obj.purchased=True;await obj.save()
        fresh=uuid.uuid4()
        await module.UUIDList.create(id=fresh,purchased=False)
        rows=await conn.execute_query_dict('SELECT id,purchased FROM uuid_list ORDER BY id')
        assert {r['id'] for r in rows}=={id.hex,fresh.hex}
        assert next(r for r in rows if r['id']==id.hex)['purchased']==1
    finally:await Tortoise.close_connections()
asyncio.run(check_storage())
print('UUID and Boolean scalar, lookup and Django storage parity passed')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), json.dumps(sys.path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") != "true" or sys.platform != "linux",
    reason="Real conversion acceptance runs in disposable Linux CI.",
)
def test_uuid_boolean_generic_crud_http_and_database_parity(tmp_path: Path) -> None:
    from test_native_fastapi import FIXTURES, _generate, _run_probe

    project = tmp_path / "project"
    shutil.copytree(FIXTURES / "drf_crud_project", project)
    models = project / "inventory/models.py"
    models.write_text(
        models.read_text()
        .replace("from django.db import models", "import uuid\nfrom django.db import models")
        .replace(
            "class Gadget(models.Model):",
            "class Gadget(models.Model):\n"
            "    id = models.UUIDField(primary_key=True, default=uuid.uuid4)",
        )
        .replace("models.PositiveIntegerField(default=0)", "models.BooleanField()")
    )
    serializer = project / "inventory/serializers.py"
    serializer.write_text(
        serializer.read_text().replace("    quantity = serializers.IntegerField(min_value=0)\n", "")
    )
    (project / "inventory/views.py").write_text("""
from rest_framework.generics import ListCreateAPIView, RetrieveUpdateDestroyAPIView
from inventory.models import Gadget
from inventory.serializers import GadgetSerializer
class GadgetList(ListCreateAPIView):
    queryset = Gadget.objects.all()
    serializer_class = GadgetSerializer
class GadgetDetail(RetrieveUpdateDestroyAPIView):
    queryset = Gadget.objects.all()
    serializer_class = GadgetSerializer
    lookup_url_kwarg = 'item_pk'
""")
    (project / "crud_config/urls.py").write_text("""
from django.urls import path
from inventory.views import GadgetList, GadgetDetail
urlpatterns = [path('api/gadgets/', GadgetList.as_view()),
    path('api/parents/<str:pk>/gadgets/<str:item_pk>/', GadgetDetail.as_view())]
""")
    output = _generate(project)
    first = "00000000-0000-0000-0000-000000000001"
    second = "00000000-0000-0000-0000-000000000002"
    detail = f"/api/parents/different-parent/gadgets/{first}/"
    scenarios = [
        {"method": "GET", "path": "/api/gadgets/"},
        {"method": "GET", "path": detail},
        {"method": "OPTIONS", "path": detail},
        {"method": "PATCH", "path": detail, "body": {"quantity": "true"}},
        {"method": "PATCH", "path": detail, "body": {"quantity": "invalid"}},
        {"method": "PATCH", "path": detail, "body": {"quantity": None}},
        {"method": "PUT", "path": detail, "body": {"name": "Changed", "quantity": "off"}},
        {"method": "PUT", "path": detail, "body": {"name": "Missing Boolean"}},
        {
            "method": "POST",
            "path": "/api/gadgets/",
            "body": {"id": second, "name": "New", "quantity": True},
        },
        {
            "method": "POST",
            "path": "/api/gadgets/",
            "body": {"id": "invalid", "name": "Bad", "quantity": False},
        },
        {"method": "GET", "path": "/api/gadgets/"},
        {"method": "DELETE", "path": detail},
        {"method": "GET", "path": detail},
        {"method": "GET", "path": "/api/parents/parent/gadgets/invalid/"},
    ]
    seed = {"id": first, "name": "Alpha", "quantity": False, "notes": ""}
    original = _run_probe(
        "source", project, tmp_path / "source.sqlite3", scenarios=scenarios, seed=seed
    )
    generated = _run_probe(
        "native",
        project,
        tmp_path / "target.sqlite3",
        output=output,
        scenarios=scenarios,
        seed=seed,
    )
    # Automatically generated IDs differ across processes. Compare their canonical
    # type and consistent API/database identity before normalizing just that value.
    from uuid import UUID

    for report in (original, generated):
        created = [row for row in report["database"] if row["name"] in {"New", "Bad"}]
        assert len(created) == 2
        for row in created:
            identifier = row["id"]
            assert str(UUID(identifier)) == identifier
            matches = [
                result["body"]
                for result in report["results"]
                if result["status"] == 201 and result["body"]["name"] == row["name"]
            ]
            assert len(matches) == 1 and matches[0]["id"] == identifier
            # Replace only this generated ID, preserving every other response value.
            report_text = json.dumps(report).replace(identifier, "created-" + row["name"])
            report.clear()
            report.update(json.loads(report_text))
    assert generated == original

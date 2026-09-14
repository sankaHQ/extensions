# SPDX-License-Identifier: Apache-2.0
"""Native CRUD must preserve nested validation and transaction rollback."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_lifecycle import call


def project(root, defaults=False):
    app = root / "catalog"
    app.mkdir()
    (app / "__init__.py").write_text("")
    (root / "settings.py").write_text(
        'SECRET_KEY="fixture"\nINSTALLED_APPS=["django.contrib.auth","django.contrib.contenttypes","catalog"]\n'
        "MIDDLEWARE=[]\n"
        'ROOT_URLCONF="urls"\nALLOWED_HOSTS=["testserver","localhost"]\n'
        'DATABASES={"default":{"ENGINE":"django.db.backends.sqlite3","NAME":"db.sqlite3"}}\n'
        'REST_FRAMEWORK={"UNAUTHENTICATED_USER":None}\n'
    )
    (app / "models.py").write_text("""from django.db import models
class Bundle(models.Model):
    code = models.CharField(max_length=20, unique=True)
    state = models.CharField(max_length=10,
                             choices=[('draft','Draft'),('ready','Ready')], default='draft')
    class Meta:
        ordering=['id']
class Part(models.Model):
    bundle = models.ForeignKey(Bundle, related_name='parts', on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField()
    cost = models.DecimalField(max_digits=6, decimal_places=2)
    class Meta:
        ordering=['id']
""")
    (app / "serializers.py").write_text("""from django.db import transaction
from rest_framework import serializers
from catalog.models import Bundle, Part
class PartSerializer(serializers.ModelSerializer):
    quantity = serializers.IntegerField(min_value=1)
    class Meta:
        model=Part
        fields=['id','quantity','cost']
        read_only_fields=['id']
class BundleSerializer(serializers.ModelSerializer):
    parts=PartSerializer(many=True)
    class Meta:
        model=Bundle
        fields=['id','code','state','parts']
        read_only_fields=['id']
    def create(self, validated_data):
        parts=validated_data.pop('parts')
        with transaction.atomic():
            bundle=Bundle.objects.create(**validated_data)
            for part in parts:
                Part.objects.create(bundle=bundle,**part)
            if sum(p.quantity for p in bundle.parts.all()) > 12:
                raise serializers.ValidationError({'parts':['Capacity exceeded.']})
        return bundle
    def update(self, instance, validated_data):
        validated_data.pop('parts',None)
        return super().update(instance,validated_data)
""")
    (root / "urls.py").write_text("""from django.urls import include, path
from rest_framework.viewsets import ModelViewSet
from rest_framework.routers import DefaultRouter
from rest_framework.renderers import JSONRenderer
from rest_framework.parsers import JSONParser
from catalog.models import Bundle
from catalog.serializers import BundleSerializer
class Bundles(ModelViewSet):
    queryset=Bundle.objects.all()
    serializer_class=BundleSerializer
    authentication_classes=[]
    renderer_classes=[JSONRenderer]
    parser_classes=[JSONParser]
router=DefaultRouter()
router.register('bundles', Bundles, basename='bundle')
urlpatterns=[path('api/',include(router.urls))]
""")

    if defaults:
        path = root / "urls.py"
        path.write_text(
            path.read_text()
            .replace("    authentication_classes=[]\n", "")
            .replace("    renderer_classes=[JSONRenderer]\n", "")
            .replace("    parser_classes=[JSONParser]\n", "")
        )


@pytest.mark.parametrize("defaults", [False, True])
def test_native_nested_crud(tmp_path: Path, defaults):
    project(tmp_path, defaults)
    scan = call(tmp_path, "scan")
    assert scan["outcome"] == "success", scan
    plan = call(tmp_path, "plan")["data"]
    assert plan["needs_adaptation_routes"] == 0, [
        (r["source_path"], r["reasons"]) for r in plan["routes"]
    ]
    applied = call(tmp_path, "apply", {"extension_plan_hash": plan["plan_hash"]}, "reviewed")
    assert applied["outcome"] == "success", applied
    output = Path(applied["data"]["output"])
    probe = """import sys
import target_app
from django.db import connection
from catalog.models import Bundle, Part
with connection.schema_editor() as editor:
    editor.create_model(Bundle)
    editor.create_model(Part)
client=target_app.app.test_client()
assert client.get('/api/').json=={'bundles':'http://localhost/api/bundles/'}
assert client.get('/api/bundles.json').status_code==200
assert client.get('/api/bundles.xml').status_code==404
assert client.options('/api/bundles/').status_code==200
r=client.post('/api/bundles/',json={'code':'alpha','parts':[{'quantity':2,'cost':'3.40'}]})
assert r.status_code == 201, (r.status_code,r.data)
assert r.json == {'id':1,'code':'alpha','state':'draft',
                  'parts':[{'id':1,'quantity':2,'cost':'3.40'}]},r.json
assert client.get('/api/bundles/1/').json == r.json
assert len(client.get('/api/bundles/').json)==1
assert client.patch('/api/bundles/1/',json={'state':'ready'}).json['state']=='ready'
r=client.post('/api/bundles/',json={'code':'rollback','parts':[{'quantity':13,'cost':'1.00'}]})
assert r.status_code==400 and r.json=={'parts':['Capacity exceeded.']},r.data
assert Bundle.objects.count()==1 and Part.objects.count()==1
r=client.post('/api/bundles/',json={'code':'alpha','parts':[]})
assert r.status_code==400 and 'code' in r.json,r.data
r=client.post('/api/bundles/',json={'code':'bad','parts':[{'quantity':0,'cost':'1.234'}]})
errors = r.json['parts']
error = errors[0] if isinstance(errors,list) else errors['0']
assert r.status_code==400 and 'quantity' in error and 'cost' in error,r.data
assert client.delete('/api/bundles/1/').status_code==204
assert Part.objects.count()==0
assert not any(m=='rest_framework' or m.startswith('rest_framework.') for m in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=output,
        env=os.environ | {"PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_json_http_and_database_match_source(tmp_path: Path) -> None:
    import json

    project(tmp_path, defaults=True)
    assert call(tmp_path, "scan")["outcome"] == "success"
    plan = call(tmp_path, "plan")["data"]
    applied = call(tmp_path, "apply", {"extension_plan_hash": plan["plan_hash"]}, "reviewed")
    assert applied["outcome"] == "success", applied
    output = Path(applied["data"]["output"])
    script = """import os, json
if os.environ['SIDE']=='source':
    os.environ['DJANGO_SETTINGS_MODULE']='settings'
    import django
    django.setup()
    from django.test import Client
    client=Client()
else:
    import target_app
    client=target_app.app.test_client()
from catalog.models import Bundle, Part
from django.contrib.auth.models import User
from django.db import connection
with connection.schema_editor() as editor:
    editor.create_model(Bundle)
    editor.create_model(Part)
    editor.create_model(User)
User.objects.create_user(username='reader',password='test-password')
results=[]
def send(method,path,data=None,authorization=None):
    if os.environ['SIDE']=='source':
        headers={'HTTP_ACCEPT':'application/json','HTTP_HOST':'localhost'}
        if authorization: headers['HTTP_AUTHORIZATION']=authorization
        r=client.generic(method,path,data=json.dumps(data) if data is not None else '',
                         content_type='application/json',**headers)
        payload=json.loads(r.content) if r.content else None
    else:
        headers={'Accept':'application/json'}
        if authorization: headers['Authorization']=authorization
        r=client.open(path,method=method,json=data,headers=headers)
        payload=r.json if r.data else None
    results.append([r.status_code,payload,r.headers.get('Allow'),
                    Bundle.objects.count(),Part.objects.count()])
send('POST','/api/bundles/',{'code':'valid','parts':[{'quantity':2,'cost':'1.20'}]})
send('GET','/api/bundles/1/')
send('GET','/api/bundles.json')
send('HEAD','/api/bundles/1/')
send('PATCH','/api/bundles/1/',{'state':'ready'})
send('PUT','/api/bundles/1/',{'code':'changed','parts':[{'quantity':1,'cost':'8.00'}]})
for data in [[],{}, {'code':'invalid','parts':[{'quantity':0,'cost':'12.345'}]},
             {'code':'rollback','parts':[{'quantity':13,'cost':'1.00'}]},
             {'code':'changed','parts':[]}, {'code':'null','parts':None},
             {'code':'choice','state':'missing','parts':[]},
             {'code':'x'*21,'parts':[]},
             {'code':'baddecimal','parts':[{'quantity':1,'cost':'NaN'}]}]:
    send('POST','/api/bundles/',data)
send('GET','/api/bundles/not-an-id/')
send('GET','/api/bundles/',authorization='Basic')
send('GET','/api/bundles/',authorization='Basic bm9ib2R5Ondyb25n')
send('GET','/api/bundles/',authorization='Basic cmVhZGVyOnRlc3QtcGFzc3dvcmQ=')
send('DELETE','/api/bundles/1/')
print(json.dumps(results,sort_keys=True))
"""
    results = []
    for side, cwd in (("source", tmp_path), ("flask", output)):
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=cwd,
            env=os.environ | {"PYTHONPATH": str(tmp_path), "SIDE": side},
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr
        results.append(json.loads(result.stdout))
    assert results[0] == results[1]


@pytest.mark.parametrize("mutation", ["permissions", "queryset", "serializer", "settings"])
def test_custom_behavior_stays_blocked(tmp_path: Path, mutation: str) -> None:
    project(tmp_path, defaults=True)
    if mutation == "permissions":
        path = tmp_path / "urls.py"
        path.write_text(
            path.read_text().replace(
                "class Bundles(ModelViewSet):",
                "from rest_framework.permissions import IsAuthenticated\n"
                "class Bundles(ModelViewSet):\n    permission_classes=[IsAuthenticated]",
            )
        )
    elif mutation == "queryset":
        path = tmp_path / "urls.py"
        path.write_text(
            path.read_text().replace("Bundle.objects.all()", "Bundle.objects.filter(state='ready')")
        )
    elif mutation == "serializer":
        path = tmp_path / "catalog/serializers.py"
        path.write_text(
            path.read_text().replace(
                "class BundleSerializer(serializers.ModelSerializer):",
                "class BundleSerializer(serializers.ModelSerializer):\n"
                "    def validate(self, attrs):\n"
                "        raise serializers.ValidationError('denied')",
            )
        )
    else:
        with (tmp_path / "settings.py").open("a") as stream:
            stream.write("\nREST_FRAMEWORK['DEFAULT_THROTTLE_RATES']={'user':'1/hour'}\n")
    assert call(tmp_path, "scan")["outcome"] == "success"
    plan = call(tmp_path, "plan")["data"]
    assert plan["needs_adaptation_routes"] > 0

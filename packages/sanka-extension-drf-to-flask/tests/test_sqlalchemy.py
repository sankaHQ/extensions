# SPDX-License-Identifier: Apache-2.0
"""Real source capture and standalone synchronous SQLAlchemy target regression."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from sanka_code_migration.drf.model import FrameworkScan

from sanka_extension_drf_to_flask.database import render_database
from sanka_extension_drf_to_flask.sqlalchemy import qualify_routes, render_sqlalchemy


@pytest.mark.parametrize("module_prefix", ["", "backend"])
def test_standalone_sqlalchemy_crud_and_contracts(tmp_path: Path, module_prefix: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    app = source / "catalog"
    app.mkdir()
    (app / "__init__.py").write_text("")
    (source / "settings.py").write_text("""
SECRET_KEY = 'fixture-only'
INSTALLED_APPS = ['rest_framework', 'catalog']
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
ROOT_URLCONF = 'urls'
MIDDLEWARE = []
ALLOWED_HOSTS = ['testserver', 'localhost']
DEFAULT_AUTO_FIELD = 'django.db.models.AutoField'
USE_TZ = True
TIME_ZONE = 'UTC'
REST_FRAMEWORK = {'DEFAULT_AUTHENTICATION_CLASSES': [], 'UNAUTHENTICATED_USER': None,
 'DEFAULT_RENDERER_CLASSES': ['rest_framework.renderers.JSONRenderer'],
 'DEFAULT_PARSER_CLASSES': ['rest_framework.parsers.JSONParser']}
""")
    (app / "models.py").write_text("""
import uuid
from django.db import models
class Product(models.Model):
    name = models.CharField(max_length=12, unique=True)
    quantity = models.IntegerField(default=7)
    enabled = models.BooleanField(default=True)
    identifier = models.UUIDField(null=True)
    stable_token = models.UUIDField(default=uuid.UUID(int=8))
    price = models.DecimalField(max_digits=6, decimal_places=2)
    posted = models.DateTimeField()
    choice = models.CharField(max_length=1, choices=[('a', 'A'), ('b', 'B')])
    hidden = models.CharField(max_length=20, default='internal')
    class Meta:
        ordering = ['id']
""")
    (app / "views.py").write_text("""
from rest_framework import serializers, viewsets, generics
from .models import Product
class ProductSerializer(serializers.ModelSerializer):
    quantity = serializers.IntegerField(default=11)
    class Meta:
        model = Product
        fields = ('id', 'name', 'quantity', 'enabled', 'identifier', 'stable_token', 'price',
                  'posted', 'choice')
class ProductViewSet(viewsets.ModelViewSet):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer
class ProductDetail(generics.RetrieveUpdateDestroyAPIView):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer
    lookup_url_kwarg = 'item_pk'
""")
    (source / "urls.py").write_text("""
from rest_framework.routers import DefaultRouter
from catalog.views import ProductViewSet, ProductDetail
from django.urls import path
router = DefaultRouter()
router.register('products', ProductViewSet)
urlpatterns = [path('parents/<str:pk>/products/<str:item_pk>/', ProductDetail.as_view())]
urlpatterns += router.urls
""")
    capture = """
import json, sys
from sanka_code_migration.drf.scan import scan_django
from sanka_code_migration.drf.models import capture_schema
from sanka_extension_drf_to_flask.sqlalchemy import capture_sqlalchemy_overrides
scan = scan_django(sys.argv[1], settings_module='settings')
from catalog.models import Product
from django.db import connection
from rest_framework.test import APIClient
with connection.schema_editor() as editor:
    editor.create_model(Product)
client = APIClient()
body = {'name': 'parity', 'enabled': False, 'price': '12.30',
        'posted': '2026-09-16T12:00:00Z', 'choice': 'a'}
without_defaults = {key: value for key, value in body.items() if key != 'enabled'}
cases = [('POST', '/products/', body), ('PUT', '/products/1/', without_defaults),
         ('PATCH', '/products/1/', {'quantity': 0}),
         ('PATCH', '/products/1/', {'enabled': None}), ('PATCH', '/products/1/', {}),
         ('PATCH', '/products/1/', {'posted': '2026-9-6 3:4:5'}),
         ('PUT', '/products/1/', body), ('POST', '/products/', body),
         ('GET', '/parents/different-parent/products/1/', None),
         ('PATCH', '/parents/different-parent/products/1/', {'quantity': 5}),
         ('GET', '/', None), ('GET', '/.json', None),
         ('GET', '/?format=json', None), ('GET', '/?other=x', None),
         ('GET', '/products/?format=', None), ('GET', '/products/?format=.json', None),
         ('GET', '/products.json?format=xml', None),
         ('GET', '/products.json', None), ('GET', '/products.json/', None),
         ('OPTIONS', '/products/', None), ('OPTIONS', '/products/999/', None),
         ('HEAD', '/products/', None), ('PUT', '/products/', None),
         ('TRACE', '/products/1/', None), ('GET', '/products/999/', None),
         ('GET', '/products', None)]
responses = []
for method, path, body in cases:
    response = client.generic(method, path, json.dumps(body) if body is not None else '',
                              content_type='application/json')
    data = response.content.decode()
    if data and response.headers.get('Content-Type') == 'application/json':
        data = json.loads(data)
    responses.append({'status': response.status_code, 'body': data,
                      'allow': response.headers.get('Allow')})
raw_cases = [(b'{', 'application/json'), (b'{"quantity":NaN}', 'application/json'),
    (b'{"quantity":Infinity}', 'application/json'),
    (bytes.fromhex('efbbbf7b7d'), 'application/json'),
    ('{}'.encode('utf-16'), 'application/json'),
    ('{"name":"caf' + chr(233) + '"}', 'application/json; charset=iso-8859-1'),
    (b'{}', 'text/plain; charset=utf-8')]
raw_cases = [(data.encode('iso-8859-1') if isinstance(data, str) else data, media)
             for data, media in raw_cases]
raw_responses = []
for data, media in raw_cases:
    response = client.generic('PATCH', '/products/1/', data, content_type=media)
    raw_responses.append({'status': response.status_code, 'body': json.loads(response.content)})
raw_cases = [(data.hex(), media) for data, media in raw_cases]
assert responses[0]['status'] == 201
overrides = capture_sqlalchemy_overrides(scan)
from catalog.views import ProductSerializer, ProductViewSet
from rest_framework import serializers
old_field = ProductSerializer._declared_fields['quantity']
def forbidden_default():
    raise AssertionError('default callable must never be executed during capture')
ProductSerializer._declared_fields['quantity'] = serializers.IntegerField(default=forbidden_default)
assert any(g['feature'] == 'serializer-default'
           for g in capture_sqlalchemy_overrides(scan)['gaps'])
ProductSerializer._declared_fields['quantity'] = old_field
from django.urls import register_converter, path
import urls
class CustomConverter:
    regex = '[0-9]+'
    def to_python(self, value):
        return 9
    def to_url(self, value):
        return str(value)
register_converter(CustomConverter, 'custom')
urls.urlpatterns.append(path('custom/<custom:pk>/',
                            ProductViewSet.as_view({'get': 'retrieve'})))
assert any(g['feature'] == 'route-converter'
           for g in capture_sqlalchemy_overrides(scan)['gaps'])
print(json.dumps({'scan': scan.to_dict(), 'schema': capture_schema([Product]),
                  'overrides': overrides,
                  'cases': cases, 'responses': responses, 'raw_cases': raw_cases,
                  'raw_responses': raw_responses}))
"""
    env = dict(os.environ)
    result = subprocess.run(
        [sys.executable, "-c", capture, str(source)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        timeout=60,
    )
    facts = json.loads(result.stdout)
    scan = FrameworkScan.from_dict(facts["scan"])
    assert all(route["native"] for route in qualify_routes(scan, facts["overrides"]))
    files = {
        **render_database(facts["schema"], module_prefix=module_prefix),
        **render_sqlalchemy(
            scan, facts["schema"], overrides=facts["overrides"], module_prefix=module_prefix
        ),
    }
    assert all(not row["native"] for row in qualify_routes(scan))
    protected = replace(
        scan,
        routes=tuple(replace(route, authentication=("unknown.Auth",)) for route in scan.routes),
    )
    assert all(
        "authentication" in row["gaps"] for row in qualify_routes(protected, facts["overrides"])
    )
    writable_relation = replace(
        scan,
        serializer_details=tuple(
            replace(
                serializer,
                fields=tuple(
                    replace(field, kind="related_pk", read_only=False)
                    if field.name == "id"
                    else field
                    for field in serializer.fields
                ),
            )
            for serializer in scan.serializer_details
        ),
    )
    assert any(
        "writable-related-field:id" in r["gaps"]
        for r in qualify_routes(writable_relation, facts["overrides"])
    )
    gap = {**facts["overrides"], "gaps": [{"feature": "serializer-default"}]}
    assert all(not row["native"] for row in qualify_routes(scan, gap))
    portable = replace(
        scan,
        source="/different/source",
        scan_hash="different",
        database=replace(scan.database, name="/different/database"),
    )
    assert render_sqlalchemy(
        portable, facts["schema"], overrides=facts["overrides"], module_prefix=module_prefix
    ) == {
        key: value
        for key, value in files.items()
        if key not in render_database(facts["schema"], module_prefix=module_prefix)
    }
    target = tmp_path / "target"
    target.mkdir()
    for name, content in files.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
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
class NoSource(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in {'django', 'rest_framework', 'fastapi', 'catalog',
                                  'sanka_code_migration', 'sanka_extension_drf_to_flask'}:
            raise ImportError('source dependency forbidden: ' + name)
sys.meta_path.insert(0, NoSource())
from target_app import create_app
app = create_app({'TESTING': True})
assert app is not create_app({'TESTING': True})
client = app.test_client()
body = {'name': 'first', 'enabled': False, 'identifier': '12345678-1234-5678-1234-567812345678',
        'price': '12.30', 'posted': '2026-09-16T12:00:00Z', 'choice': 'a'}
r = client.post('/products/', json=body)
assert r.status_code == 201, (r.status_code, r.data)
assert r.json == {'id': 1, 'quantity': 11,
                  'stable_token': '00000000-0000-0000-0000-000000000008', **body}, r.json
without_defaults = {key: value for key, value in body.items() if key != 'enabled'}
preserved = client.put('/products/1/', json=without_defaults).json
assert preserved['enabled'] is False
assert preserved['stable_token'] == '00000000-0000-0000-0000-000000000008'
assert client.patch('/products/1/', json={}).json['enabled'] is False
assert client.patch('/products/1/', json={'quantity': 0}).json['quantity'] == 0
assert client.patch('/products/1/', json={'enabled': None}).json == {
    'enabled': ['This field may not be null.']}
assert client.patch('/products/1/', json={'name': ''}).json == {
    'name': ['This field may not be blank.']}
assert client.post('/products/', json=body).json == {
    'name': ['product with this name already exists.']}
assert client.get('/products.json').status_code == 200
assert client.get('/products.json/').json[0]['quantity'] == 0
assert client.get('/products.xml/').status_code == 404
assert client.head('/products/').data == b''
assert client.put('/products/').status_code == 405
assert client.put('/products/').json == {'detail': 'Method "PUT" not allowed.'}
assert client.options('/products/').json['actions']['POST']['name']['required'] is True
assert 'PUT' not in client.options('/products/999/').json.get('actions', {})
assert client.get('/products').status_code == 404
assert client.get('/products', follow_redirects=False).headers.get('Location') is None
assert client.get('/products/999/').json == {
    'detail': 'No Product matches the given query.'}
assert client.delete('/products/1/').status_code == 204
assert client.get('/products/').json == []
assert client.get('/products/', headers={'Accept': 'text/html'}).status_code == 406
assert client.post('/products/', data='x', content_type='text/plain').status_code == 415
assert not {'django', 'rest_framework', 'fastapi'}.intersection(sys.modules)
print('standalone CRUD verified')
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

    (target / "reference.json").write_text(json.dumps(facts))
    env["SANKA_DATABASE_URL"] = "sqlite:///" + str(target / "parity.sqlite3")
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    parity = """
import json
from pathlib import Path
from target_app import create_app
facts = json.loads(Path('reference.json').read_text())
client = create_app({'TESTING': True}).test_client()
for (method, path, body), expected in zip(facts['cases'], facts['responses'], strict=True):
    response = client.open(path, method=method,
        data=json.dumps(body) if body is not None else '', content_type='application/json',
        base_url='http://testserver')
    data = response.get_data(as_text=True)
    if data and response.headers.get('Content-Type') == 'application/json':
        data = json.loads(data)
    observed = {'status': response.status_code, 'body': data,
                'allow': response.headers.get('Allow')}
    assert observed == expected, (method, path, observed, expected)
for (data, media), expected in zip(facts['raw_cases'], facts['raw_responses'], strict=True):
    response = client.patch('/products/1/', data=bytes.fromhex(data), content_type=media)
    observed = {'status': response.status_code, 'body': response.json}
    assert observed == expected, (data, media, observed, expected)
"""
    result = subprocess.run(
        [sys.executable, "-c", parity],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr

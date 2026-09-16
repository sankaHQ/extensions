# SPDX-License-Identifier: Apache-2.0
"""Token/owner HTTP and denied-write parity against live DRF fixtures."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from sanka_code_migration.drf.model import FrameworkScan

from sanka_extension_drf_to_flask.database import render_database
from sanka_extension_drf_to_flask.sqlalchemy import qualify_routes, render_sqlalchemy


def test_native_token_owner_contract_and_denied_writes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    app = source / "catalog"
    app.mkdir(parents=True)
    (app / "__init__.py").write_text("")
    (source / "settings.py").write_text("""
SECRET_KEY = 'fixture-only'
INSTALLED_APPS = ['django.contrib.auth', 'django.contrib.contenttypes', 'rest_framework', 'catalog']
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
ROOT_URLCONF = 'urls'
AUTH_USER_MODEL = 'catalog.Account'
MIDDLEWARE = []
ALLOWED_HOSTS = ['testserver']
DEFAULT_AUTO_FIELD = 'django.db.models.AutoField'
USE_TZ = True
TIME_ZONE = 'UTC'
REST_FRAMEWORK = {'DEFAULT_AUTHENTICATION_CLASSES': [], 'UNAUTHENTICATED_USER': None,
 'DEFAULT_RENDERER_CLASSES': ['rest_framework.renderers.JSONRenderer'],
 'DEFAULT_PARSER_CLASSES': ['rest_framework.parsers.JSONParser']}
""")
    (app / "models.py").write_text("""
from django.db import models
from django.contrib.auth.base_user import AbstractBaseUser
class Account(AbstractBaseUser):
    username = models.CharField(max_length=30, unique=True)
    is_active = models.BooleanField(default=True)
    is_superuser = models.BooleanField(default=False)
    USERNAME_FIELD = 'username'
class Credential(models.Model):
    key = models.CharField(max_length=40, primary_key=True)
    user = models.ForeignKey(Account, on_delete=models.DO_NOTHING)
class Post(models.Model):
    author = models.ForeignKey(Account, on_delete=models.DO_NOTHING)
    title = models.CharField(max_length=40)
    class Meta:
        ordering = ['id']
""")
    (app / "views.py").write_text("""
from rest_framework import serializers, viewsets, permissions
from rest_framework.authentication import TokenAuthentication
from .models import Post, Credential
TokenAuthentication.model = Credential
class OwnerOrReadOnly(permissions.BasePermission):
    message = "Only the author may change this post."
    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return True
        return obj.author_id == request.user.id
class PostSerializer(serializers.ModelSerializer):
    class Meta:
        model = Post
        fields = ('id', 'author', 'title')
        read_only_fields = ('id', 'author')
class PostViewSet(viewsets.ModelViewSet):
    queryset = Post.objects.all()
    serializer_class = PostSerializer
    authentication_classes = (TokenAuthentication,)
    permission_classes = (permissions.IsAuthenticated, OwnerOrReadOnly)
    def perform_create(self, serializer):
        serializer.save(author=self.request.user)
""")
    (source / "urls.py").write_text("""
from rest_framework.routers import SimpleRouter
from catalog.views import PostViewSet
router = SimpleRouter()
router.register('posts', PostViewSet)
urlpatterns = router.urls
""")
    capture = """
import json, sys
from sanka_code_migration.drf.scan import scan_django
from sanka_code_migration.drf.models import capture_schema
from sanka_extension_drf_to_flask.sqlalchemy import capture_sqlalchemy_overrides
scan = scan_django(sys.argv[1], settings_module='settings')
from catalog.models import Account, Credential, Post
from django.db import connection
from rest_framework.test import APIClient
with connection.schema_editor() as editor:
    for model in (Account, Credential, Post):
        editor.create_model(model)
for pk, name, active in [(1, 'alice', True), (2, 'bob', True), (3, 'inactive', False)]:
    user = Account.objects.create(id=pk, username=name, is_active=active)
    Credential.objects.create(key=name, user=user)
Post.objects.create(author_id=1, title='alice original')
Post.objects.create(author_id=2, title='bob original')
cases = [
 ('GET', '/posts/', None, ''),
 ('GET', '/posts/', None, 'Basic ignored'),
 ('GET', '/posts/', None, 'Token'),
 ('GET', '/posts/', None, 'Token a b'),
 ('GET', '/posts/', None, 'Token unknown'),
 ('GET', '/posts/', None, 'Token inactive'),
 ('GET', '/posts/', None, 'Token bob'),
 ('OPTIONS', '/posts/', None, ''),
 ('POST', '/posts/', {'title': 'injected owner', 'author': 2}, 'Token alice'),
 ('PATCH', '/posts/1/', {'title': 'forbidden'}, 'Token bob'),
 ('DELETE', '/posts/1/', None, 'Token bob'),
 ('GET', '/posts/1/', None, 'Token bob'),
 ('PATCH', '/posts/1/', {'title': 'updated'}, 'Token alice'),
 ('PATCH', '/posts/2/', {'title': 'bob updated'}, 'Token bob'),
 ('OPTIONS', '/posts/1/', None, 'Token bob'),
 ('OPTIONS', '/posts/1/', None, 'Token alice'),
 ('POST', '/posts/1/', {}, 'Token unknown'),
 ('POST', '/posts/1/', {}, 'Token bob'),
 ('GET', '/posts/999/', None, 'Token unknown'),
 ('GET', '/posts/999/', None, 'Token alice'),
 ('GET', '/posts/', None, 'token alice'),
 ('GET', '/posts/', None, 'Token ÿ'),
 ('GET', '/posts/', None, 'Token' + chr(160) + 'alice'),
 ('PATCH', '/posts/1/', {'title': 'denied whitespace'}, 'Token alice' + chr(160)),
]
client = APIClient()
responses = []
for method, path, body, header in cases:
    response = client.generic(method, path, json.dumps(body) if body is not None else '',
                              content_type='application/json', HTTP_AUTHORIZATION=header)
    responses.append({'status': response.status_code, 'body': json.loads(response.content),
        'allow': response.headers.get('Allow'),
        'authenticate': response.headers.get('WWW-Authenticate'),
        'database': list(Post.objects.order_by('id').values('id', 'author_id', 'title'))})
assert responses[8]['status'] == 201
assert responses[9]['status'] == responses[10]['status'] == 403
assert responses[9]['database'] == responses[8]['database'] == responses[10]['database']
from catalog.views import PostSerializer
from sanka_code_migration.drf.scan import _serializer_field_ir
original_readonly = PostSerializer.Meta.read_only_fields
PostSerializer.Meta.read_only_fields = ('id',)
writable_author = PostSerializer().fields['author']
assert writable_author.run_validation(1).pk == 1
assert not _serializer_field_ir('author', writable_author, Post).supported
PostSerializer.Meta.read_only_fields = original_readonly
print(json.dumps({'scan': scan.to_dict(), 'schema': capture_schema([Post, Credential]),
                  'overrides': capture_sqlalchemy_overrides(scan),
                  'cases': cases, 'responses': responses}))
"""
    env = dict(os.environ)
    result = subprocess.run(
        [sys.executable, "-c", capture, str(source)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    facts = json.loads(result.stdout)
    assert facts["schema"]["gaps"] == []
    scan = FrameworkScan.from_dict(facts["scan"])
    qualified = qualify_routes(scan, facts["overrides"])
    assert all(row["native"] for row in qualified), qualified
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
    env["SANKA_DATABASE_URL"] = "sqlite:///" + str(target / "auth.sqlite3")
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
        if name.split('.')[0] in {'django', 'rest_framework', 'fastapi', 'catalog',
                                 'sanka_code_migration', 'sanka_extension_drf_to_flask'}:
            raise ImportError(name)
sys.meta_path.insert(0, NoSource())
import sqlalchemy as sa
from target_app import create_app
from models import TABLES
app = create_app({'TESTING': True})
engine = app.extensions['sanka_engine']
with engine.begin() as connection:
    for pk, name, active in [(1, 'alice', True), (2, 'bob', True), (3, 'inactive', False)]:
        connection.execute(sa.insert(TABLES['catalog_account']).values(
            id=pk, username=name, is_active=active, is_superuser=False, password=''))
        connection.execute(sa.insert(TABLES['catalog_credential']).values(key=name, user_id=pk))
    connection.execute(sa.insert(TABLES['catalog_post']), [
        {'id': 1, 'author_id': 1, 'title': 'alice original'},
        {'id': 2, 'author_id': 2, 'title': 'bob original'}])
facts = json.loads(Path('reference.json').read_text())
client = app.test_client()
for (method, path, body, header), expected in zip(facts['cases'], facts['responses'], strict=True):
    response = client.open(path, method=method,
        data=json.dumps(body) if body is not None else '', content_type='application/json',
        headers={'Authorization': header}, base_url='http://testserver')
    with engine.connect() as connection:
        rows = [dict(row) for row in connection.execute(sa.select(TABLES['catalog_post']).
                                                       order_by(TABLES['catalog_post'].c.id)).mappings()]
    observed = {'status': response.status_code, 'body': response.json,
        'allow': response.headers.get('Allow'),
        'authenticate': response.headers.get('WWW-Authenticate'),
        'database': rows}
    assert observed == expected, (method, path, header, observed, expected)
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

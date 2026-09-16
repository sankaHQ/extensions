# SPDX-License-Identifier: Apache-2.0
"""Stock database session authentication is captured without source secrets."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from sanka_code_migration.drf.model import FrameworkScan

from sanka_extension_drf_to_flask.database import render_database
from sanka_extension_drf_to_flask.sqlalchemy import render_sqlalchemy


def _session_project(root: Path) -> None:
    app = root / "catalog"
    app.mkdir(parents=True)
    (app / "__init__.py").write_text("")
    (root / "settings.py").write_text(
        """
SECRET_KEY = 'fixture-current-secret'
SECRET_KEY_FALLBACKS = ['fixture-old-secret']
INSTALLED_APPS = ['django.contrib.auth', 'django.contrib.contenttypes',
                  'django.contrib.sessions', 'rest_framework', 'catalog']
import json, os
DATABASES = {'default': json.loads(os.environ['SANKA_SOURCE_TEST_DATABASE'])}
ROOT_URLCONF = 'urls'
AUTH_USER_MODEL = 'catalog.Account'
AUTHENTICATION_BACKENDS = ['django.contrib.auth.backends.ModelBackend']
MIDDLEWARE = [
 'django.contrib.sessions.middleware.SessionMiddleware',
 'django.contrib.auth.middleware.AuthenticationMiddleware',
 'django.middleware.csrf.CsrfViewMiddleware',
]
ALLOWED_HOSTS = ['testserver', 'trusted.example']
DEFAULT_AUTO_FIELD = 'django.db.models.AutoField'
USE_TZ = True
TIME_ZONE = 'UTC'
SESSION_ENGINE = 'django.contrib.sessions.backends.db'
SESSION_SERIALIZER = 'django.contrib.sessions.serializers.JSONSerializer'
SESSION_COOKIE_NAME = 'project_session'
SESSION_COOKIE_AGE = 1800
SESSION_COOKIE_DOMAIN = None
SESSION_COOKIE_PATH = '/'
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
CSRF_USE_SESSIONS = False
CSRF_COOKIE_NAME = 'project_csrf'
CSRF_COOKIE_AGE = 3600
CSRF_COOKIE_DOMAIN = None
CSRF_COOKIE_PATH = '/'
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = 'Lax'
CSRF_HEADER_NAME = 'HTTP_X_PROJECT_CSRF'
CSRF_TRUSTED_ORIGINS = ['https://trusted.example']
REST_FRAMEWORK = {
 'UNAUTHENTICATED_USER': None,
 'DEFAULT_RENDERER_CLASSES': ['rest_framework.renderers.JSONRenderer'],
 'DEFAULT_PARSER_CLASSES': ['rest_framework.parsers.JSONParser'],
}
"""
    )
    (app / "models.py").write_text(
        """
from django.db import models
from django.contrib.auth.base_user import AbstractBaseUser
class Account(AbstractBaseUser):
    username = models.CharField(max_length=30, unique=True)
    is_active = models.BooleanField(default=True)
    is_superuser = models.BooleanField(default=False)
    USERNAME_FIELD = 'username'
class Note(models.Model):
    text = models.CharField(max_length=40)
"""
    )
    (app / "views.py").write_text(
        """
from rest_framework import permissions, serializers, viewsets
from rest_framework.authentication import SessionAuthentication
from .models import Note
class NoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Note
        fields = ('id', 'text')
        read_only_fields = ('id',)
class NoteViewSet(viewsets.ModelViewSet):
    queryset = Note.objects.all()
    serializer_class = NoteSerializer
    authentication_classes = (SessionAuthentication,)
    permission_classes = (permissions.IsAuthenticated,)
"""
    )
    (root / "urls.py").write_text(
        """
from rest_framework.routers import SimpleRouter
from catalog.views import NoteViewSet
router = SimpleRouter()
router.register('notes', NoteViewSet)
urlpatterns = router.urls
"""
    )


def test_stock_session_contract_is_explicit_and_secret_free(tmp_path: Path) -> None:
    _session_project(tmp_path)
    script = """
import django, json
django.setup()
from catalog.views import NoteViewSet
from sanka_extension_drf_to_flask.sqlalchemy_sessions import capture_session_auth
contract = capture_session_auth(NoteViewSet)
print(json.dumps(contract, sort_keys=True))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=os.environ
        | {
            "DJANGO_SETTINGS_MODULE": "settings",
            "SANKA_SOURCE_TEST_DATABASE": json.dumps(
                {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
            ),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    contract = json.loads(result.stdout)
    assert contract["kind"] == "session"
    assert contract["secret_env"] == "SANKA_DJANGO_SECRET_KEY"
    assert contract["fallbacks_env"] == "SANKA_DJANGO_SECRET_KEY_FALLBACKS"
    assert contract["fallback_count"] == 1
    assert contract["session"]["cookie_name"] == "project_session"
    assert contract["csrf"]["trusted_origins"] == ["https://trusted.example"]
    serialized = json.dumps(contract, sort_keys=True)
    assert "fixture-current-secret" not in serialized
    assert "fixture-old-secret" not in serialized


def _capture_rejection(root: Path, script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=os.environ
        | {
            "DJANGO_SETTINGS_MODULE": "settings",
            "SANKA_SOURCE_TEST_DATABASE": json.dumps(
                {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
            ),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_custom_private_auth_hash_is_rejected(tmp_path: Path) -> None:
    _session_project(tmp_path)
    result = _capture_rejection(
        tmp_path,
        """
import django
django.setup()
from catalog.models import Account
from catalog.views import NoteViewSet
from sanka_extension_drf_to_flask.sqlalchemy_sessions import capture_session_auth
Account._get_session_auth_hash = lambda self, secret=None: 'custom'
try:
    capture_session_auth(NoteViewSet)
except ValueError as error:
    print(error)
else:
    raise AssertionError('custom hash was accepted')
""",
    )
    assert result.returncode == 0, result.stderr
    assert "custom session auth hash" in result.stdout


def test_custom_user_manager_lookup_is_rejected(tmp_path: Path) -> None:
    _session_project(tmp_path)
    model_path = tmp_path / "catalog" / "models.py"
    model_path.write_text(
        model_path.read_text().replace(
            "class Account(AbstractBaseUser):",
            "class RejectingManager(models.Manager):\n"
            "    def get(self, *args, **kwargs): raise self.model.DoesNotExist\n"
            "class Account(AbstractBaseUser):\n"
            "    objects = RejectingManager()",
        )
    )
    result = _capture_rejection(
        tmp_path,
        """
import django
django.setup()
from catalog.views import NoteViewSet
from sanka_extension_drf_to_flask.sqlalchemy_sessions import capture_session_auth
try:
    capture_session_auth(NoteViewSet)
except ValueError as error:
    print(error)
else:
    raise AssertionError('custom manager lookup was accepted')
""",
    )
    assert result.returncode == 0, result.stderr
    assert "custom user manager lookup" in result.stdout


def test_session_middleware_without_session_view_is_blocking(tmp_path: Path) -> None:
    _session_project(tmp_path)
    views = tmp_path / "catalog" / "views.py"
    views.write_text(
        views.read_text()
        .replace("from rest_framework.authentication import SessionAuthentication\n", "")
        .replace(
            "authentication_classes = (SessionAuthentication,)\n"
            "    permission_classes = (permissions.IsAuthenticated,)",
            "authentication_classes = ()\n    permission_classes = (permissions.AllowAny,)",
        )
    )
    result = _capture_rejection(
        tmp_path,
        """
import django
django.setup()
from sanka_code_migration.drf.scan import scan_django
from sanka_extension_drf_to_flask.sqlalchemy import capture_sqlalchemy_overrides
facts = capture_sqlalchemy_overrides(scan_django('.', settings_module='settings'))
assert {'source':'MIDDLEWARE','feature':'unqualified-session-middleware'} in facts['gaps']
""",
    )
    assert result.returncode == 0, result.stderr


def test_mixed_public_and_session_views_are_blocking(tmp_path: Path) -> None:
    _session_project(tmp_path)
    views = tmp_path / "catalog" / "views.py"
    views.write_text(
        views.read_text() + "\nclass PublicViewSet(NoteViewSet):\n"
        "    authentication_classes = ()\n"
        "    permission_classes = (permissions.AllowAny,)\n"
    )
    urls = tmp_path / "urls.py"
    urls.write_text(
        urls.read_text()
        .replace(
            "from catalog.views import NoteViewSet",
            "from catalog.views import NoteViewSet, PublicViewSet",
        )
        .replace(
            "router.register('notes', NoteViewSet)",
            "router.register('notes', NoteViewSet)\n"
            "router.register('public', PublicViewSet, basename='public')",
        )
    )
    result = _capture_rejection(
        tmp_path,
        """
import django
django.setup()
from sanka_code_migration.drf.scan import scan_django
from sanka_extension_drf_to_flask.sqlalchemy import capture_sqlalchemy_overrides, qualify_routes
scan = scan_django('.', settings_module='settings')
rows = qualify_routes(scan, capture_sqlalchemy_overrides(scan))
public = [row for row in rows if '/public' in row['path']]
assert public and all(not row['native'] for row in public), public
assert all('mixed-session-authentication' in row['gaps'] for row in public), public
""",
    )
    assert result.returncode == 0, result.stderr


def test_session_contract_qualifies_and_is_emitted_without_secrets(tmp_path: Path) -> None:
    _session_project(tmp_path)
    script = """
import django, json
django.setup()
from sanka_code_migration.drf.scan import scan_django
from sanka_code_migration.drf.models import capture_schema
from sanka_extension_drf_to_flask.sqlalchemy import (
    capture_sqlalchemy_overrides, qualify_routes, render_sqlalchemy,
)
scan = scan_django('.', settings_module='settings')
overrides = capture_sqlalchemy_overrides(scan)
qualified = qualify_routes(scan, overrides)
schema = capture_schema()
files = render_sqlalchemy(scan, schema, overrides=overrides)
print(json.dumps({'qualified': qualified, 'contract': json.loads(files['native_contract.json']),
                  'files': sorted(files)}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=os.environ
        | {
            "DJANGO_SETTINGS_MODULE": "settings",
            "SANKA_SOURCE_TEST_DATABASE": json.dumps(
                {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
            ),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert all(route["native"] for route in observed["qualified"]), observed["qualified"]
    auth = next(iter(observed["contract"]["views"].values()))["auth"]
    assert auth["kind"] == "session"
    assert "sanka_native/sqlalchemy_sessions.py" in observed["files"]
    assert "fixture-current-secret" not in json.dumps(observed)


def test_database_session_auth_and_csrf_match_source(tmp_path: Path, contract_databases) -> None:
    source = tmp_path / "source"
    _session_project(source)
    source_database, target_url = contract_databases
    env = os.environ | {
        "DJANGO_SETTINGS_MODULE": "settings",
        "SANKA_SOURCE_TEST_DATABASE": json.dumps(source_database),
    }
    capture = r"""
import datetime, django, json
django.setup()
from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.db import connection
from django.test import Client, override_settings
from django.utils import timezone
from django.middleware.csrf import _mask_cipher_secret
from catalog.models import Account, Note
from sanka_code_migration.drf.models import capture_schema
from sanka_code_migration.drf.scan import scan_django
from sanka_extension_drf_to_flask.sqlalchemy import capture_sqlalchemy_overrides

with connection.schema_editor() as editor:
    for model in (Account, Note, Session): editor.create_model(model)
alice = Account.objects.create(id=1, username='alice', password='current-password',
                               is_active=True, is_superuser=False)
inactive = Account.objects.create(id=2, username='inactive', password='inactive-password',
                                  is_active=False, is_superuser=False)

def data(user, auth_hash=None):
    return {SESSION_KEY: str(user.pk), BACKEND_SESSION_KEY:
            'django.contrib.auth.backends.ModelBackend',
            HASH_SESSION_KEY: auth_hash if auth_hash is not None else user.get_session_auth_hash()}
def create(key, values, secret=None):
    context = override_settings(SECRET_KEY=secret, SECRET_KEY_FALLBACKS=[]) if secret else None
    if context: context.enable()
    try:
        store=SessionStore(key); store._session_cache=values; store.save(must_create=True)
    finally:
        if context: context.disable()

for key in ('validget1','validpost1','validpost2','validpost3','validpost4','validpost5',
            'validpost6','validpost7','validpost8','validpost9','validpost10',
            'validpost11','validpost12','validpost13'):
    create(key, data(alice))
create('inactive1', data(inactive))
missing_user = data(alice); missing_user[SESSION_KEY] = '999'
create('missinguser1', missing_user)
missing_backend = data(alice); missing_backend.pop(BACKEND_SESSION_KEY)
create('missingback1', missing_backend)
expiry_60 = data(alice); expiry_60['_session_expiry'] = 60
create('expiry60a', expiry_60)
expiry_browser = data(alice); expiry_browser['_session_expiry'] = 0
create('expiry0aaa', expiry_browser)
create('badhash01', data(alice, 'wrong'))
create('inthash01', data(alice, 123))
create('unicodeh1', data(alice, 'é'))
huge_user = data(alice); huge_user[SESSION_KEY] = str(10**100)
create('hugeuser1', huge_user)
with override_settings(SECRET_KEY='fixture-old-secret', SECRET_KEY_FALLBACKS=[]):
    old_hash = alice.get_session_auth_hash()
create('fallback1', data(alice, old_hash), 'fixture-old-secret')
Session.objects.create(session_key='corrupt01', session_data='not-signed',
                       expire_date=timezone.now()+datetime.timedelta(hours=1))
create('expired01', data(alice))
Session.objects.filter(session_key='expired01').update(
    expire_date=timezone.now()-datetime.timedelta(seconds=1))
initial_sessions=list(Session.objects.order_by('session_key').values(
    'session_key','session_data','expire_date'))
for row in initial_sessions: row['expire_date']=row['expire_date'].isoformat()

secret='A'*32; masked=_mask_cipher_secret(secret)
cases = [
 {'id':'anonymous','method':'GET','key':None},
 {'id':'missing','method':'GET','key':'missing01'},
 {'id':'corrupt','method':'GET','key':'corrupt01'},
 {'id':'expired','method':'GET','key':'expired01'},
 {'id':'inactive','method':'GET','key':'inactive1'},
 {'id':'valid-get','method':'GET','key':'validget1'},
 {'id':'csrf-missing','method':'POST','key':'validpost1','body':{'text':'missing'}},
 {'id':'csrf-wrong','method':'POST','key':'validpost2','csrf':secret,'header':'B'*32,
  'body':{'text':'wrong'}},
 {'id':'csrf-unmasked','method':'POST','key':'validpost3','csrf':secret,'header':secret,
  'body':{'text':'unmasked'}},
 {'id':'csrf-masked','method':'POST','key':'validpost4','csrf':secret,'header':masked,
  'body':{'text':'masked'}},
 {'id':'bad-origin','method':'POST','key':'validpost5','csrf':secret,'header':secret,
  'secure':True,'origin':'https://evil.example','body':{'text':'origin'}},
 {'id':'trusted-origin','method':'POST','key':'validpost6','csrf':secret,'header':secret,
  'secure':True,'origin':'https://trusted.example','body':{'text':'trusted'}},
 {'id':'no-referer','method':'POST','key':'validpost7','csrf':secret,'header':secret,
  'secure':True,'body':{'text':'referer'}},
 {'id':'same-origin-referer','method':'POST','key':'validpost8','csrf':secret,'header':secret,
  'secure':True,'referer':'https://testserver/form','body':{'text':'same-origin'}},
 {'id':'malformed-header','method':'POST','key':'validpost9','csrf':secret,'header':'short',
  'body':{'text':'malformed-header'}},
 {'id':'malformed-cookie','method':'POST','key':'validpost10','csrf':'short','header':secret,
  'body':{'text':'malformed-cookie'}},
 {'id':'masked-cookie','method':'POST','key':'validpost11','csrf':masked,'header':secret,
  'body':{'text':'masked-cookie'}},
 {'id':'bad-referer','method':'POST','key':'validpost12','csrf':secret,'header':secret,
  'secure':True,'referer':'https://evil.example/form','body':{'text':'bad-referer'}},
 {'id':'invalid-header-characters','method':'POST','key':'validpost13','csrf':secret,
  'header':'A'*31+'!','body':{'text':'invalid-header'}},
 {'id':'missing-user','method':'GET','key':'missinguser1'},
 {'id':'missing-backend','method':'GET','key':'missingback1'},
 {'id':'custom-expiry','method':'GET','key':'expiry60a'},
 {'id':'browser-close-expiry','method':'GET','key':'expiry0aaa'},
 {'id':'bad-hash','method':'GET','key':'badhash01'},
 {'id':'non-string-hash','method':'GET','key':'inthash01'},
 {'id':'unicode-hash','method':'GET','key':'unicodeh1'},
 {'id':'out-of-range-user','method':'GET','key':'hugeuser1'},
 {'id':'fallback','method':'GET','key':'fallback1'},
]
responses=[]
for case in cases:
    client=Client(enforce_csrf_checks=True)
    if case.get('key'): client.cookies[settings.SESSION_COOKIE_NAME]=case['key']
    if case.get('csrf'): client.cookies[settings.CSRF_COOKIE_NAME]=case['csrf']
    headers={}
    if case.get('header'): headers[settings.CSRF_HEADER_NAME]=case['header']
    if case.get('origin'): headers['HTTP_ORIGIN']=case['origin']
    if case.get('referer'): headers['HTTP_REFERER']=case['referer']
    response=client.generic(case['method'],'/notes/',
        json.dumps(case.get('body')) if 'body' in case else '', content_type='application/json',
        secure=case.get('secure',False), **headers)
    body=json.loads(response.content) if response.content else None
    cookie=response.cookies.get(settings.SESSION_COOKIE_NAME)
    followup_status=client.get('/notes/').status_code if case['id']=='fallback' else None
    responses.append({'id':case['id'],'status':response.status_code,'body':body,
      'vary':response.headers.get('Vary'),
      'session_cookie': None if cookie is None else {
        'value_present':bool(cookie.value),'max_age':cookie['max-age'],'path':cookie['path'],
        'domain':cookie['domain'],'secure':bool(cookie['secure']),
        'httponly':bool(cookie['httponly']),'samesite':cookie['samesite']},
      'original_session_exists': bool(case.get('key') and
          Session.objects.filter(session_key=case['key']).exists()),
      'followup_status':followup_status,
      'notes':list(Note.objects.order_by('id').values('id','text'))})
scan=scan_django('.',settings_module='settings')
print(json.dumps({'scan':scan.to_dict(),'schema':capture_schema([Account,Note,Session]),
 'overrides':capture_sqlalchemy_overrides(scan),'sessions':initial_sessions,
 'cases':cases,'responses':responses},default=str))
"""
    source_result = subprocess.run(
        [sys.executable, "-c", capture],
        cwd=source,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert source_result.returncode == 0, source_result.stderr
    facts = json.loads(source_result.stdout)
    scan = FrameworkScan.from_dict(facts["scan"])
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
    target_env = os.environ | {
        "SANKA_DATABASE_URL": target_url,
        "SANKA_DJANGO_SECRET_KEY": "fixture-current-secret",
        "SANKA_DJANGO_SECRET_KEY_FALLBACKS": json.dumps(["fixture-old-secret"]),
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=target,
        env=target_env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    probe = r"""
import datetime, importlib.abc, json, sys
from pathlib import Path
class NoSource(importlib.abc.MetaPathFinder):
 def find_spec(self,name,path=None,target=None):
  if name.split('.')[0] in {'django','rest_framework','catalog','sanka_code_migration',
                            'sanka_extension_drf_to_flask'}: raise ImportError(name)
sys.meta_path.insert(0,NoSource())
import sqlalchemy as sa
from target_app import create_app
from models import TABLES
facts=json.loads(Path('reference.json').read_text())
app=create_app({'TESTING':True});engine=app.extensions['sanka_engine']
with engine.begin() as connection:
 connection.execute(sa.insert(TABLES['catalog_account']),[
  {'id':1,'username':'alice','password':'current-password','is_active':True,'is_superuser':False},
  {'id':2,'username':'inactive','password':'inactive-password','is_active':False,'is_superuser':False}])
 for row in facts['sessions']:
  row=dict(row);row['expire_date']=datetime.datetime.fromisoformat(row['expire_date'])
  connection.execute(sa.insert(TABLES['django_session']).values(**row))
responses=[]
for case in facts['cases']:
 client=app.test_client(use_cookies=False);headers={}
 cookies=[]
 if case.get('key'): cookies.append('project_session='+case['key'])
 if case.get('csrf'): cookies.append('project_csrf='+case['csrf'])
 if cookies: headers['Cookie']='; '.join(cookies)
 if case.get('header'): headers['X-Project-Csrf']=case['header']
 if case.get('origin'): headers['Origin']=case['origin']
 if case.get('referer'): headers['Referer']=case['referer']
 response=client.open('/notes/',method=case['method'],
  data=json.dumps(case.get('body')) if 'body' in case else '',content_type='application/json',
  base_url=('https://' if case.get('secure') else 'http://')+'testserver',headers=headers)
 with engine.connect() as connection:
  notes=[dict(row) for row in connection.execute(sa.select(TABLES['catalog_note']).order_by(
      TABLES['catalog_note'].c.id)).mappings()]
  exists=bool(case.get('key') and connection.scalar(sa.select(sa.func.count()).select_from(
      TABLES['django_session']).where(TABLES['django_session'].c.session_key==case['key'])))
 cookie=response.headers.getlist('Set-Cookie');session_cookie=next(
  (value for value in cookie if value.startswith('project_session=')),None)
 parsed=None
 followup_status=None
 if session_cookie is not None:
  from http.cookies import SimpleCookie
  jar=SimpleCookie();jar.load(session_cookie);item=jar['project_session']
  parsed={'value_present':bool(item.value),
   'max_age':int(item['max-age']) if item['max-age'] else '', 'path':item['path'],
   'domain':item['domain'],'secure':bool(item['secure']),'httponly':bool(item['httponly']),
   'samesite':item['samesite']}
  if case['id']=='fallback':
   followup_status=client.get('/notes/',headers={'Cookie':'project_session='+item.value}).status_code
 responses.append({'id':case['id'],'status':response.status_code,'body':response.json,
  'vary':response.headers.get('Vary'),'session_cookie':parsed,
  'original_session_exists':exists,'followup_status':followup_status,'notes':notes})
if responses != facts['responses']:
 print(json.dumps([{'target':target,'source':source} for target,source in
  zip(responses,facts['responses'],strict=True) if target != source],indent=2))
 raise AssertionError('session response mismatch')
engine.dispose()
"""
    target_result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=target,
        env=target_env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert target_result.returncode == 0, target_result.stdout + target_result.stderr

# SPDX-License-Identifier: Apache-2.0
"""Compare middleware ordering and short-circuit behavior with live Django."""

import os
import subprocess
import sys


def test_stock_middleware_matches_source_order_and_redirects(tmp_path):
    (tmp_path / "settings.py").write_text(
        "SECRET_KEY='test'\nROOT_URLCONF='urls'\nINSTALLED_APPS=[]\n"
        "ALLOWED_HOSTS=['testserver']\nMIDDLEWARE=[]\n"
        "SECURE_HSTS_SECONDS=123\nSECURE_HSTS_INCLUDE_SUBDOMAINS=True\nSECURE_HSTS_PRELOAD=True\n"
    )
    (tmp_path / "urls.py").write_text(
        "from django.urls import path\nfrom django.http import JsonResponse\n"
        "def index(request): return JsonResponse({'ok': True})\n"
        "urlpatterns=[path('items/', index)]\n"
    )
    script = """
import django, itertools, json, re
django.setup()
from django.conf import settings
from django.test import Client, override_settings
from flask import Flask, Response
from sanka_code_migration.drf.scan import _capture_http_security
from sanka_extension_drf_to_flask.sqlalchemy import capture_middleware
from sanka_extension_drf_to_flask.sqlalchemy_middleware import install_middleware

middleware = [
 'django.middleware.common.CommonMiddleware',
 'django.middleware.security.SecurityMiddleware',
 'django.middleware.clickjacking.XFrameOptionsMiddleware',
]
headers = ['Location', 'Content-Type', 'Content-Length', 'X-Frame-Options',
 'Strict-Transport-Security', 'Referrer-Policy', 'X-Content-Type-Options',
 'Cross-Origin-Opener-Policy']
cases = [(path, method, secure, host)
 for path in ['/items/', '/items?x=1', '/absent', '//items?next=%2F%2Fevil']
 for method in ['GET', 'POST', 'HEAD'] for secure in [True, False]
 for host in ['testserver', 'evil.example']]
for order in itertools.permutations(middleware):
 for ssl in [False, True]:
  with override_settings(MIDDLEWARE=list(order), SECURE_SSL_REDIRECT=ssl):
   source = Client()
   facts = capture_middleware()
   contract = {'middleware': facts, 'http_security': _capture_http_security(settings, order)}
   app = Flask(__name__)
   app.url_map.strict_slashes = False
   app.url_map.redirect_defaults = False
   @app.errorhandler(404)
   def absent(error):
    from django.views.defaults import page_not_found
    from django.test import RequestFactory
    page = page_not_found(RequestFactory().get('/absent'), Exception())
    return Response(page.content, 404, content_type=page['Content-Type'])
   # Native dispatcher has a catch-all, so Flask never performs its own slash redirect.
   def dispatch(rest):
    if rest == 'items/':
     return Response(json.dumps({'ok': True}), content_type='application/json')
    return absent(None)
   app.add_url_rule('/<path:rest>', 'fallback', dispatch, methods=['GET','POST'])
   install_middleware(app, contract, [(re.compile('items/$'), {'append_slash': True})])
   target = app.test_client()
   for path, method, secure, host in cases:
    original = source.generic(method, path, secure=secure, HTTP_HOST=host)
    migrated = target.open(path, method=method, base_url=('https://' if secure else 'http://')+host)
    expected = (original.status_code, original.content.decode(),
                {h:original.get(h) for h in headers})
    actual = (migrated.status_code, migrated.get_data().decode(),
              {h:migrated.headers.get(h) for h in headers})
    assert actual == expected, (order, ssl, path, method, secure, host, expected, actual)
print('ok')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=os.environ | {"DJANGO_SETTINGS_MODULE": "settings"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"

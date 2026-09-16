# SPDX-License-Identifier: Apache-2.0
"""Compare native CSRF checks and cookie repair with the real DRF CSRF checker."""

import json
import os
import string
import subprocess
import sys
from http.cookies import SimpleCookie
from pathlib import Path

import pytest
from flask import Flask, jsonify
from test_sqlalchemy_sessions import _session_project

from sanka_extension_drf_to_flask import sqlalchemy_sessions as runtime


@pytest.mark.parametrize("cookie_domain", [None, ".example.com"])
def test_csrf_origin_referer_and_cookie_contract(tmp_path: Path, cookie_domain):
    _session_project(tmp_path)
    secret = "A" * 32
    common = {"method": "POST", "cookie": secret, "token": secret}
    cases = [
        common | {"id": "valid"},
        common | {"id": "empty-origin", "origin": ""},
        common | {"id": "malformed-origin", "origin": "https://["},
        common | {"id": "wildcard", "origin": "https://child.example.com:8443"},
        common | {"id": "wrong-port", "origin": "https://child.example.com:9443"},
        common | {"id": "wrong-scheme", "origin": "http://child.example.com:8443"},
        common | {"id": "untrusted-host", "host": "evil.example", "origin": "http://evil.example"},
        common | {"id": "https", "secure": True, "referer": "https://testserver/page"},
        common
        | {"id": "cookie-domain", "secure": True, "referer": "https://child.example.com/page"},
        common | {"id": "trusted-referer", "secure": True, "referer": "https://plain.example/page"},
        common
        | {
            "id": "wrong-referer-port",
            "secure": True,
            "referer": "https://child.example.com:9443/page",
        },
        common | {"id": "malformed-referer", "secure": True, "referer": "https://["},
        common | {"id": "empty-referer", "secure": True, "referer": ""},
        common | {"id": "no-referer", "secure": True},
        common | {"id": "missing-cookie", "cookie": None},
        common | {"id": "empty-cookie", "cookie": "", "token": None},
        common | {"id": "short-cookie", "cookie": "short", "token": None},
        common | {"id": "bad-cookie", "cookie": "!" * 32},
        common | {"id": "safe-bad-cookie", "method": "GET", "cookie": "short"},
        common | {"id": "empty-token", "token": ""},
        common | {"id": "missing-token", "token": None},
        common | {"id": "bad-token", "token": "!" * 32},
        common | {"id": "short-token", "token": "short"},
        common | {"id": "masked-cookie", "cookie": "A" * 32 + "0" * 32},
    ]
    source = r"""
import django, json, sys
django.setup()
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from rest_framework.authentication import CSRFCheck
from catalog.views import NoteViewSet
from sanka_extension_drf_to_flask.sqlalchemy_sessions import capture_session_auth
values=json.loads(sys.stdin.read())
with override_settings(CSRF_COOKIE_DOMAIN=values['domain'],
        CSRF_TRUSTED_ORIGINS=['https://*.example.com:8443', 'http://plain.example']):
    contract=capture_session_auth(NoteViewSet)
    results=[]
    for case in values['cases']:
        headers={'HTTP_HOST':case.get('host','testserver')}
        for key,name in [
            ('origin','HTTP_ORIGIN'), ('referer','HTTP_REFERER'),
            ('token','HTTP_X_CSRFTOKEN')]:
            if case.get(key) is not None: headers[name]=case[key]
        request=RequestFactory().generic(case['method'],'/notes/',secure=case.get('secure',False),**headers)
        if case.get('cookie') is not None: request.COOKIES['project_csrf']=case['cookie']
        check=CSRFCheck(lambda request: HttpResponse())
        check.process_request(request)
        reason=check.process_view(request,None,(),{})
        response=check.process_response(request,HttpResponse())
        results.append({'reason':reason,'vary':response.headers.get('Vary'),
            'cookie':response.cookies.output(header='').strip()})
    print(json.dumps({'contract':contract,'results':results}))
"""
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=tmp_path,
        env=os.environ
        | {
            "DJANGO_SETTINGS_MODULE": "settings",
            "SANKA_SOURCE_TEST_DATABASE": json.dumps(
                {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
            ),
        },
        input=json.dumps({"domain": cookie_domain, "cases": cases}),
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    facts = json.loads(result.stdout)
    context = {"facts": facts["contract"]}
    app = Flask(__name__)

    @app.before_request
    def prepare():
        runtime.prepare_csrf(context)

    @app.route("/notes/", methods=["GET", "POST"])
    def handle():
        return jsonify({"reason": runtime._csrf_failure(context)})

    @app.after_request
    def finish(response):
        return runtime.save_csrf(context, response)

    def cookie_shape(raw):
        cookies = SimpleCookie(raw)
        if not cookies:
            return None
        cookie = cookies["project_csrf"]
        assert len(cookie.value) == 32
        assert set(cookie.value) <= set(string.ascii_letters + string.digits)
        return {
            name: cookie[name]
            for name in ("max-age", "domain", "path", "secure", "httponly", "samesite")
        } | {"expires": bool(cookie["expires"])}

    for case, expected in zip(cases, facts["results"], strict=True):
        headers = {}
        for key, name in [("origin", "Origin"), ("referer", "Referer"), ("token", "X-CSRFToken")]:
            if case.get(key) is not None:
                headers[name] = case[key]
        if case.get("cookie") is not None:
            headers["Cookie"] = "project_csrf=" + case["cookie"]
        base = ("https" if case.get("secure") else "http") + "://" + case.get("host", "testserver")
        response = app.test_client(use_cookies=False).open(
            "/notes/", method=case["method"], base_url=base, headers=headers
        )
        assert response.status_code == 200, case
        assert response.json == {"reason": expected["reason"]}, case
        assert response.headers.get("Vary") == expected["vary"], case
        assert cookie_shape(response.headers.get("Set-Cookie", "")) == cookie_shape(
            expected["cookie"]
        ), case

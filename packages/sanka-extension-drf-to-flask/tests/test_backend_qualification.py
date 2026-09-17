# SPDX-License-Identifier: Apache-2.0
"""Bounded synthetic backends exercised through the public extension protocol."""

import json
import os
from pathlib import Path

import pytest
from test_lifecycle import call
from test_model_viewsets import project as nested_project
from test_native_plan import native_project
from test_sqlalchemy_sessions import _session_project

POSTGRES_ENV = "SANKA_MIGRATE_TEST_POSTGRES_DSN"


def request(identifier, method, path, status, body=None, **extra):
    result = {
        "id": identifier,
        "method": method,
        "path": path,
        "expected_source_status": status,
        **extra,
    }
    if body is not None:
        result["body"] = body
    return result


def configure_database(settings, backend):
    with settings.open("a") as out:
        out.write("""
import os
from urllib.parse import urlsplit, unquote
_value = os.environ.get('SANKA_TEST_DB', 'unused.sqlite3')
if _value.startswith(('postgresql://', 'postgres://')):
    _url = urlsplit(_value)
    DATABASES = {'default': {'ENGINE': 'django.db.backends.postgresql',
        'NAME': unquote(_url.path.lstrip('/')), 'USER': unquote(_url.username or ''),
        'PASSWORD': unquote(_url.password or ''), 'HOST': _url.hostname or '',
        'PORT': _url.port or 5432}}
else:
    DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': _value}}
""")
        if backend == "postgresql":
            out.write(
                "\nif 'SANKA_TEST_DB' not in os.environ:\n"
                "    DATABASES = {'default': {'ENGINE': 'django.db.backends.postgresql', "
                "'NAME': 'unused'}}\n"
            )


def inventory(root):
    native_project(root)
    base = "/api/gadgets/"
    valid = {"name": "widget", "quantity": 2}
    create = {"method": "POST", "path": base, "body": valid}
    return (
        "crud_config.settings",
        "minimal",
        [
            request("create", "POST", base, 201, valid),
            request("invalid", "POST", base, 400, {"name": "widget", "quantity": -1}),
            request("read", "GET", base + "1/", 200, setup=[create]),
            request("update", "PATCH", base + "1/", 200, {"quantity": 3}, setup=[create]),
            request("delete", "DELETE", base + "1/", 204, setup=[create]),
            request("missing", "GET", base + "999/", 404),
        ],
        None,
    )


def sessions(root):
    _session_project(root)
    settings = root / "settings.py"
    settings.write_text(
        settings.read_text().replace(
            "json.loads(os.environ['SANKA_SOURCE_TEST_DATABASE'])",
            "{'ENGINE': 'django.db.backends.sqlite3', 'NAME': 'unused.sqlite3'}",
        )
    )
    cookie = "project_session=validsession1; project_csrf=" + "A" * 32
    auth = {"Cookie": cookie, "X-Project-Csrf": "A" * 32}
    scenarios = [
        request("anonymous", "GET", "/notes/", 403),
        request("authorized", "GET", "/notes/", 200, headers=auth),
        request(
            "csrf-denied", "POST", "/notes/", 403, {"text": "blocked"}, headers={"Cookie": cookie}
        ),
        request("create", "POST", "/notes/", 201, {"text": "accepted"}, headers=auth),
        request("invalid", "POST", "/notes/", 400, {"text": ""}, headers=auth),
        request("update", "PATCH", "/notes/1/", 200, {"text": "changed"}, headers=auth),
        request("delete", "DELETE", "/notes/1/", 204, headers=auth),
    ]
    seed = """from catalog.models import Account, Note
from django.contrib.auth import SESSION_KEY, BACKEND_SESSION_KEY, HASH_SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
user = Account.objects.create(username='alice', password='fixture', is_active=True)
Note.objects.create(text='seed')
store = SessionStore('validsession1')
store._session_cache = {SESSION_KEY: str(user.pk),
    BACKEND_SESSION_KEY: 'django.contrib.auth.backends.ModelBackend',
    HASH_SESSION_KEY: user.get_session_auth_hash()}
store.save(must_create=True)
"""
    return "settings", "full", scenarios, seed


def orders(root):
    nested_project(root)
    settings = root / "settings.py"
    settings.write_text(
        settings.read_text().replace(
            '["django.contrib.auth","django.contrib.contenttypes","catalog"]', '["catalog"]'
        )
        + "\nDEFAULT_AUTO_FIELD='django.db.models.AutoField'\n"
        "REST_FRAMEWORK.update({'DEFAULT_AUTHENTICATION_CLASSES': [],"
        "'DEFAULT_RENDERER_CLASSES': ['rest_framework.renderers.JSONRenderer'],"
        "'DEFAULT_PARSER_CLASSES': ['rest_framework.parsers.JSONParser']})\n"
    )
    serializers = root / "catalog/serializers.py"
    serializers.write_text(
        serializers.read_text().replace(
            "if sum(p.quantity for p in bundle.parts.all()) > 12:",
            "total = sum(p.quantity for p in bundle.parts.all())\n            if total > 12:",
        )
    )
    base = "/api/bundles/"
    valid = {"code": "first", "parts": [{"quantity": 2, "cost": "3.40"}]}
    create = {"method": "POST", "path": base, "body": valid}
    rollback = {
        "method": "POST",
        "path": base,
        "body": {"code": "rollback", "parts": [{"quantity": 13, "cost": "1.00"}]},
    }
    return (
        "settings",
        "auto",
        [
            request("create", "POST", base, 201, valid),
            request("relational-read", "GET", base + "1/", 200, setup=[create]),
            request("rollback", "POST", base, 400, rollback["body"], setup=[create]),
            request(
                "recovery",
                "POST",
                base,
                201,
                {"code": "recovered", "parts": [{"quantity": 3, "cost": "2.50"}]},
                setup=[create, rollback],
            ),
            request(
                "invalid",
                "POST",
                base,
                400,
                {"code": "bad", "parts": [{"quantity": 0, "cost": "1.234"}]},
            ),
            request("update", "PATCH", base + "1/", 200, {"state": "ready"}, setup=[create]),
            request("cascade-delete", "DELETE", base + "1/", 204, setup=[create]),
        ],
        None,
    )


@pytest.mark.parametrize("database_backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("profile", [inventory, sessions, orders], ids=lambda fn: fn.__name__)
def test_representative_backend_protocol(tmp_path, monkeypatch, database_backend, profile):
    if database_backend == "postgresql" and not os.environ.get(POSTGRES_ENV):
        pytest.skip(f"set {POSTGRES_ENV} to a dedicated PostgreSQL admin DSN")
    settings_module, generation, scenarios, seed = profile(tmp_path)
    configure_database(tmp_path / (settings_module.replace(".", "/") + ".py"), database_backend)
    if profile is sessions:
        monkeypatch.setenv("SANKA_DJANGO_SECRET_KEY", "fixture-current-secret")
        monkeypatch.setenv("SANKA_DJANGO_SECRET_KEY_FALLBACKS", '["fixture-old-secret"]')
    (tmp_path / "scenarios.json").write_text(json.dumps(scenarios))
    if seed:
        (tmp_path / "seed.py").write_text(seed)
    config = {"settings_module": settings_module, "orm": "sqlalchemy", "generation": generation}
    scanned = call(tmp_path, "scan", config)
    assert scanned["outcome"] == "success", scanned
    planned = call(tmp_path, "plan", config)
    assert planned["outcome"] == "success", planned
    plan = planned["data"]
    assert plan["needs_adaptation_routes"] == 0, plan
    assert call(tmp_path, "plan", config)["data"]["plan_hash"] == plan["plan_hash"]
    assert ("backend/models.py" in plan["files"]) is (generation != "minimal")
    assert not any("repositories" in name for name in plan["files"])
    reviewed = config | {"extension_plan_hash": plan["plan_hash"]}
    applied = call(tmp_path, "apply", reviewed, "reviewed-fixture")
    assert applied["outcome"] == "success", applied
    verify = reviewed | {
        "candidate": applied["data"]["output"],
        "scenarios": "scenarios.json",
        "database_backend": database_backend,
        "ignore_tables": ["django_admin_log", "django_migrations", "sqlite_sequence"],
    }
    if database_backend == "postgresql":
        verify["postgres_admin_dsn_env"] = POSTGRES_ENV
    if seed:
        verify["seed"] = "seed.py"
    result = call(tmp_path, "verify", verify)
    assert result["outcome"] == "success", result
    assert result["data"]["summary"]["matched"] == len(scenarios)
    assert result["data"]["summary"]["non_native"] == 0
    report = json.loads(Path(result["data"]["report_path"]).read_text())
    observed = {item["id"]: item for item in report["scenarios"]}
    assert all(
        item["source_expectation_match"] and item["database_match"] for item in observed.values()
    )
    if profile is orders:
        for name, count in [("rollback", 1), ("recovery", 2), ("cascade-delete", 0)]:
            counts = observed[name]["source"]["database_after"]
            prefix = "public." if database_backend == "postgresql" else ""
            assert counts[prefix + "catalog_bundle"] == counts[prefix + "catalog_part"] == count
    assert not (tmp_path / "unused.sqlite3").exists()

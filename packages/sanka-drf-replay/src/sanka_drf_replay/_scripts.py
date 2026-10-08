# SPDX-License-Identifier: Apache-2.0
"""Isolated Python replay process programs."""

_PREPARE_SCRIPT = r"""
import json, os, runpy, sys
payload = json.load(sys.stdin)
sys.path.insert(0, payload["project_root"])
os.environ["DJANGO_SETTINGS_MODULE"] = payload["settings_module"]
os.environ[payload["db_env"]] = payload["database"]
import django
validate_django_databases()
django.setup()
from django.core.management import call_command
call_command("migrate", interactive=False, verbosity=0, run_syncdb=True)
if payload.get("seed"):
    from django.db import OperationalError, InterfaceError
    sqlite = payload.get("database_backend") != "postgresql"
    before = os.stat(payload["database"]) if sqlite else None
    seed_error = None
    try:
        runpy.run_path(payload["seed"], run_name="__main__")
    except Exception as error:
        seed_error = error
    # Replacing an open SQLite file invalidates the migrated connection.
    try:
        after = os.stat(payload["database"]) if sqlite else None
    except FileNotFoundError:
        after = None
    replaced = sqlite and (after is None or (before.st_dev, before.st_ino) !=
                           (after.st_dev, after.st_ino))
    if replaced or (seed_error is not None and not isinstance(
            seed_error, (OSError, OperationalError, InterfaceError))):
        if sqlite and seed_error is not None:
            import traceback
            traceback.print_exception(seed_error)
        print(json.dumps({"ok": False, "failure_category": "seed_failure",
                          "database_replaced": replaced}))
        raise SystemExit(0)
    if seed_error is not None:
        raise seed_error
from django.conf import settings
if os.path.realpath(settings.MEDIA_ROOT) != os.path.realpath(payload["media_root"]):
    raise SystemExit("seed changed MEDIA_ROOT; write seed files under settings.MEDIA_ROOT")
from django.db import connections
connections.close_all()
print(json.dumps({"ok": True}))
"""

_SOURCE_SCRIPT = r"""
import base64, json, os, sys
payload = json.load(sys.stdin)
sys.path.insert(0, payload["project_root"])
os.environ["DJANGO_SETTINGS_MODULE"] = payload["settings_module"]
os.environ[payload["db_env"]] = payload["database"]
import django
validate_django_databases()
django.setup()
from django.test import Client
client = Client(enforce_csrf_checks=True)


def send(request):
    body, headers = request_bytes(request)
    content_type = headers.pop("content-type", "application/octet-stream")
    headers = {"HTTP_" + key.upper().replace("-", "_"): value
               for key, value in headers.items()}
    response = client.generic(request["method"], request["path"], data=body,
                              content_type=content_type, **headers)
    if getattr(response, "streaming", False):
        content = b"".join(response.streaming_content)
    else:
        content = bytes(response.content)
    return {"status": response.status_code,
            "headers": {str(key).lower(): str(value) for key, value in response.headers.items()},
            "body_b64": base64.b64encode(content).decode("ascii")}

for step in payload["setup"]:
    send(step)
result = send(payload["request"])
from django.db import connections
connections.close_all()
print(json.dumps(result))
"""

_CANDIDATE_SCRIPT = r"""
import base64, importlib.util, inspect, json, os, sys
payload = json.load(sys.stdin)
candidate_root = payload["candidate_root"]
# A complete candidate tree must win over same-named source packages.
sys.path[:0] = [candidate_root, payload["project_root"]]
candidate_url = (payload["database"].replace("postgresql://", "postgresql+psycopg://", 1)
                 if payload.get("database_backend") == "postgresql" and payload["target"] == "flask"
                 else payload["database"])
os.environ[payload["candidate_db_env"]] = candidate_url
if payload.get("database_backend") == "postgresql":
    os.environ["SANKA_DATABASE_URL"] = candidate_url
os.environ[payload["db_env"]] = payload["database"]
entrypoint_path = os.path.join(candidate_root, payload["entrypoint"])
spec = importlib.util.spec_from_file_location("_sanka_replay_candidate", entrypoint_path)
module = importlib.util.module_from_spec(spec)
sys.modules["_sanka_replay_candidate"] = module
spec.loader.exec_module(module)
if "django" in sys.modules:
    validate_django_databases()
app = getattr(module, "app", None)
factory_used = app is None
url = (payload["database"].replace("postgresql://", "postgresql+psycopg://", 1)
       if payload.get("database_backend") == "postgresql" else "sqlite:///" + payload["database"])
if app is None and payload["target"] == "flask" and callable(getattr(module, "create_app", None)):
    app = module.create_app({"DATABASE_URL": url, "TESTING": True})
if app is not None and payload["target"] == "flask":
    engine = app.extensions.get("sanka_engine")
    if payload.get("database_backend") == "postgresql":
        from sqlalchemy.engine import make_url
        if (engine is None and "django" not in sys.modules) or (
            engine is not None and engine.url != make_url(url)):
            raise SystemExit("candidate database must use the isolated PostgreSQL database")
    elif (factory_used and engine is None) or (engine is not None and (
          engine.url.get_backend_name() != "sqlite"
          or os.path.realpath(str(engine.url.database)) != os.path.realpath(payload["database"]))):
        raise SystemExit("candidate database must use the isolated SQLite path")
if app is None:
    raise SystemExit("candidate entrypoint does not expose `app` or a supported Flask factory")
if payload["target"] == "flask":
    from flask import Flask
    if not isinstance(app, Flask):
        raise SystemExit("candidate app is not Flask")
    client_context = app.test_client(use_cookies=False)
else:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.routing import Match
    if not isinstance(app, FastAPI):
        raise SystemExit("candidate app is not FastAPI")
    client_context = TestClient(app, follow_redirects=False)

def resolve(route, scope, depth=0):
    if route is None or depth > 16 or type(route).__qualname__ != "_IncludedRouter":
        return route
    for candidate in route.effective_candidates():
        try:
            match, _ = candidate.matches(scope)
        except Exception:
            continue
        if match == Match.FULL:
            inner = resolve(candidate, scope, depth + 1)
            return getattr(inner, "original_route", inner)
    return route

def native(method, path):
    if payload["target"] == "flask":
        from flask import request
        from werkzeug.exceptions import NotFound, MethodNotAllowed
        from werkzeug.routing import RequestRedirect
        with app.test_request_context(path, method=method, base_url="http://testserver"):
            rule = request.url_rule
            endpoint = app.view_functions.get(rule.endpoint) if rule else None
            filename = inspect.getsourcefile(inspect.unwrap(endpoint)) if endpoint else None
            framework_response = isinstance(request.routing_exception,
                                            (NotFound, MethodNotAllowed, RequestRedirect))
        forbidden = any(name == "rest_framework" or name.startswith("rest_framework.")
                        for name in sys.modules)
        inside = bool(filename) and os.path.realpath(filename).startswith(
            os.path.realpath(candidate_root) + os.sep)
        return {"is_flask": True, "endpoint_in_candidate": inside,
                "framework_response": framework_response, "forbidden_imports": forbidden,
                "default_wsgi_dispatch": getattr(app.wsgi_app, "__func__", None) is Flask.wsgi_app}
    scope = {"type": "http", "method": method, "path": path.split("?", 1)[0],
             "root_path": "", "headers": [], "query_string": b""}
    matched = None
    for route in getattr(app, "routes", []):
        match, _ = route.matches(scope)
        if match == Match.FULL:
            matched = resolve(route, scope)
            break
    if matched is None:
        return {"route_class": None, "is_apiroute": False,
                "endpoint_in_candidate": False}
    cls = type(matched)
    try:
        from fastapi.routing import APIRoute
        is_apiroute = isinstance(matched, APIRoute)
    except Exception:
        is_apiroute = False
    endpoint = getattr(matched, "endpoint", None)
    filename = None
    try:
        filename = (inspect.getsourcefile(inspect.unwrap(endpoint))
                    if endpoint is not None else None)
    except Exception:
        filename = None
    root = os.path.realpath(candidate_root) + os.sep
    inside = bool(filename) and os.path.realpath(filename).startswith(root)
    return {"route_class": cls.__module__ + "." + cls.__qualname__,
            "is_apiroute": is_apiroute, "endpoint_in_candidate": inside}

with client_context as client:
    from http.cookies import SimpleCookie
    cookies = SimpleCookie()
    def send(request):
        body, headers = request_bytes(request)
        if payload["target"] == "flask":
            if cookies and "cookie" not in headers:
                headers["cookie"] = "; ".join(sorted(
                    f"{value.key}={value.coded_value}" for value in cookies.values()))
            response = client.open(request["path"], method=request["method"],
                                   data=body, headers=headers, follow_redirects=False,
                                   base_url="http://testserver")
            for value in response.headers.getlist("Set-Cookie"):
                cookies.load(value)
            content = response.data
        else:
            response = client.request(request["method"], request["path"],
                                      content=body, headers=headers)
            content = response.content
        return {"status": response.status_code,
                "headers": {str(key).lower(): str(value)
                            for key, value in response.headers.items()},
                "body_b64": base64.b64encode(content).decode("ascii")}
    for step in payload["setup"]:
        send(step)
    result = send(payload["request"])
result["native"] = native(payload["request"]["method"], payload["request"]["path"])
print(json.dumps(result))
"""


_REQUEST_SCRIPT = r"""
import base64, json
def multipart(spec):
    boundary = str(spec.get("boundary") or payload["boundary"]).encode("ascii")
    chunks = []
    for name, value in (spec.get("fields") or {}).items():
        disposition = 'Content-Disposition: form-data; name="%s"' % name
        chunks += [b"--" + boundary, disposition.encode(), b"", str(value).encode()]
    for item in spec.get("files") or []:
        disposition = 'Content-Disposition: form-data; name="%s"; filename="%s"' % (
            item["field"], item["filename"])
        content_type = item.get("content_type") or "application/octet-stream"
        chunks += [b"--" + boundary, disposition.encode(),
                   ("Content-Type: %s" % content_type).encode("ascii"), b"",
                   base64.b64decode(str(item["content_b64"]), validate=True)]
    chunks += [b"--" + boundary + b"--", b""]
    return b"\r\n".join(chunks), boundary.decode("ascii")


def request_bytes(request):
    headers = {key.lower(): value for key, value in request.get("headers", {}).items()}
    if request.get("multipart") is not None:
        body, boundary = multipart(request["multipart"])
        headers.setdefault("content-type", "multipart/form-data; boundary=" + boundary)
    elif "body_base64" in request:
        body = base64.b64decode(request["body_base64"], validate=True)
    elif "body" in request:
        body = json.dumps(request["body"], allow_nan=False).encode("utf-8")
        headers.setdefault("content-type", "application/json")
    else:
        body = b""
        headers.setdefault("content-type", "application/json")
    return body, headers
"""
_DATABASE_SCRIPT = r"""
def validate_django_databases():
    from django.db import connections
    for connection in connections.all():
        config = connection.settings_dict
        if payload.get("database_backend") == "postgresql":
            from urllib.parse import urlsplit, unquote, parse_qsl
            expected = urlsplit(payload["database"])
            valid = (config["ENGINE"] == "django.db.backends.postgresql"
                     and str(config["NAME"]) == unquote(expected.path.lstrip("/"))
                     and str(config.get("HOST", "")) == (expected.hostname or "")
                     and str(config.get("PORT") or 5432) == str(expected.port or 5432)
                     and str(config.get("USER", "")) == unquote(expected.username or "")
                     and str(config.get("PASSWORD", "")) == unquote(expected.password or "")
                     and (config.get("OPTIONS") or {}) == dict(parse_qsl(expected.query)))
            if not valid:
                raise SystemExit("every Django alias must use the isolated PostgreSQL database")
        elif (config["ENGINE"] != "django.db.backends.sqlite3"
              or os.path.realpath(str(config["NAME"])) != os.path.realpath(payload["database"])):
            raise SystemExit("every Django database alias must use the isolated SQLite path")
"""

_MEDIA_SCRIPT = r"""
import importlib
os.environ["BENCH_MEDIA_ROOT"] = payload["media_root"]
# Configure the source settings before Django or a derived serving module imports it.
importlib.import_module(payload["settings_module"]).MEDIA_ROOT = payload["media_root"]
"""
for _script_name in ("_PREPARE_SCRIPT", "_SOURCE_SCRIPT", "_CANDIDATE_SCRIPT"):
    _script = _DATABASE_SCRIPT + globals()[_script_name]
    _marker = 'os.environ[payload["db_env"]] = payload["database"]'
    binding = _marker + "\n" + _MEDIA_SCRIPT
    if _script_name == "_CANDIDATE_SCRIPT":
        binding += '\nif payload.get("database_backend") == "postgresql":\n'
        binding += '    os.environ[payload["candidate_db_env"]] = candidate_url\n'
        binding += '    os.environ["SANKA_DATABASE_URL"] = candidate_url\n'
    globals()[_script_name] = _script.replace(_marker, binding, 1)
_SOURCE_SCRIPT = _REQUEST_SCRIPT + _SOURCE_SCRIPT
_CANDIDATE_SCRIPT = _REQUEST_SCRIPT + _CANDIDATE_SCRIPT

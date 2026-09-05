# SPDX-License-Identifier: Apache-2.0
"""Conservative native Flask generation, without importing another extension.

ponytail: JSON APIView handlers and isolated ORM models only; serializers,
authentication, middleware, and custom dispatch remain explicit manual gaps.
Extend the recognizer only with differential evidence for the additional behavior.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import inspect
import io
import json
import os
import re
import shutil
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any, cast

from sanka_extension_sdk import (
    ExtensionRequest,
    ExtensionResponse,
    JsonValue,
    failure_response,
    success_response,
)


def _hash(value: Any) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
    )


def _within(root: Path, value: str) -> Path:
    raw = root / value
    if raw.is_symlink() or any(p.is_symlink() for p in raw.parents if p != root):
        raise ValueError("output and artifact paths must not contain symlinks")
    path = raw.resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError("output must be a new directory inside the project")
    return path


def _source_hash(root: Path) -> str:
    records = {}
    for directory, names, files in os.walk(root):
        names[:] = sorted(n for n in names if not n.startswith(".") and n != "__pycache__")
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix == ".py" or name in {"pyproject.toml", "requirements.txt"}:
                if path.is_symlink():
                    raise ValueError("source files must not be symlinks")
                records[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return _hash(records)


def _settings(root: Path, config: dict[str, JsonValue]) -> str:
    value = config.get("settings_module") or os.environ.get("DJANGO_SETTINGS_MODULE")
    if not value:
        tree = ast.parse((root / "manage.py").read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and len(node.args) >= 2
                and (
                    isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "DJANGO_SETTINGS_MODULE"
                )
            ):
                value = ast.literal_eval(node.args[1])
                break
    if not isinstance(value, str) or not all(p.isidentifier() for p in value.split(".")):
        raise ValueError("provide settings_module or a literal DJANGO_SETTINGS_MODULE in manage.py")
    return value


def _flask_path(raw: str) -> str | None:
    # Regex URL patterns and converters with different matching semantics are manual gaps.
    raw = raw.replace("<str:", "<string:")
    if re.search(r"[\\^$?*+()\[\]{}|]", raw):
        return None
    converters = re.findall(r"<(?:(\w+):)?\w+>", raw)
    if any(c not in {"", "int", "string"} for c in converters):
        return None
    return "/" + raw.lstrip("/")


def _model_import(name: str, value: Any) -> str | None:
    """Reuse an ORM module only when its imports have no serving dependencies."""
    from django.db.models import Model  # type: ignore[import-untyped]

    if not inspect.isclass(value) or not issubclass(value, Model):
        return None
    module = inspect.getmodule(value)
    if module is None:
        return None
    tree = ast.parse(inspect.getsource(module))
    allowed = {"django.db", "django.db.models", "decimal", "datetime", "uuid"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(n.name not in allowed for n in node.names):
            return None
        if isinstance(node, ast.ImportFrom) and (node.level or node.module not in allowed):
            return None
        if isinstance(node, ast.Name) and node.id in {"__import__", "exec", "eval"}:
            return None
    return f"from {value.__module__} import {value.__name__} as {name}"


def _handler(view: Any, method: str) -> tuple[str | None, str]:
    from django.conf import settings  # type: ignore[import-untyped]
    from django.db import transaction  # type: ignore[import-untyped]
    from rest_framework.parsers import JSONParser  # type: ignore[import-untyped]
    from rest_framework.permissions import AllowAny  # type: ignore[import-untyped]
    from rest_framework.renderers import JSONRenderer  # type: ignore[import-untyped]
    from rest_framework.response import Response  # type: ignore[import-untyped]
    from rest_framework.views import APIView  # type: ignore[import-untyped]

    if view.__bases__ != (APIView,):
        return None, "generic views, viewsets and inherited handlers require manual migration"
    if list(view.permission_classes) != [AllowAny] or view.authentication_classes:
        return None, "authentication and permissions require manual migration"
    if view.throttle_classes or list(view.renderer_classes) != [JSONRenderer]:
        return None, "throttling and content negotiation require manual migration"
    allowed = {
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "head",
        "options",
        "permission_classes",
        "authentication_classes",
        "renderer_classes",
        "parser_classes",
    }
    if any(not name.startswith("__") and name not in allowed for name in vars(view)):
        return None, "custom view attributes or lifecycle hooks require manual migration"
    function = getattr(view, method.lower(), None)
    if function is None or method in {"HEAD", "OPTIONS"}:
        return None, "implicit HEAD and OPTIONS behavior requires manual migration"
    node = ast.parse(textwrap.dedent(inspect.getsource(function))).body[0]
    if not isinstance(node, ast.FunctionDef) or node.decorator_list:
        return None, "decorated or asynchronous handlers require manual migration"
    if [arg.arg for arg in node.args.args[:2]] != ["self", "request"]:
        return None, "nonstandard handler signature requires manual migration"
    if node.args.vararg or node.args.kwarg or node.args.defaults or node.args.kwonlyargs:
        return None, "variadic or default handler arguments require manual migration"
    node.args.args = node.args.args[2:]
    node.returns = None
    for arg in node.args.args:
        arg.annotation = None
    bound = {arg.arg for arg in node.args.args}
    bound.update(
        n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
    )
    builtins = {
        "str",
        "int",
        "float",
        "bool",
        "len",
        "list",
        "dict",
        "sorted",
        "min",
        "max",
        "sum",
        "range",
        "enumerate",
        "ValueError",
        "TypeError",
        "isinstance",
    }
    if function.__globals__.get("Response") is not Response:
        return None, "Response must be the DRF response constructor"
    if any(name in function.__globals__ for name in builtins):
        return None, "shadowed builtins require manual migration"
    used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    imports = []
    for name in sorted(used - bound - builtins - {"request", "Response"}):
        value = function.__globals__.get(name)
        statement = (
            f"from django.db import transaction as {name}"
            if value is transaction
            else _model_import(name, value)
        )
        if statement is None:
            return None, "handler depends on unsupported source globals, serializers or services"
        imports.extend(ast.parse(statement).body)
    parents = {child: parent for parent in ast.walk(node) for child in ast.iter_child_nodes(parent)}
    for item in list(ast.walk(node)):
        if isinstance(item, ast.Name) and item.id == "request":
            parent = parents.get(item)
            if not isinstance(parent, ast.Attribute) or isinstance(item.ctx, ast.Store):
                return None, "passing or rebinding request requires manual migration"
        if isinstance(item, (ast.Import, ast.ImportFrom)):
            return None, "handler-local imports require manual migration"
        if (
            isinstance(item, ast.Attribute)
            and isinstance(item.value, ast.Name)
            and item.value.id == "request"
        ):
            if item.attr not in {"query_params", "method", "data"}:
                return None, f"request.{item.attr} semantics require manual migration"
            if item.attr == "data":
                if (
                    list(view.parser_classes) != [JSONParser]
                    or not JSONParser.strict
                    or settings.DEFAULT_CHARSET.lower() != "utf-8"
                ):
                    return None, "request.data requires the unmodified JSONParser alone"
                parent = parents.get(item)
                if not isinstance(item.ctx, ast.Load):
                    return None, "rebinding request.data requires manual migration"
                # NodeTransformer below replaces the complete attribute with a call.
            if item.attr == "query_params":
                parent = parents.get(item)
                call = parents.get(parent) if parent is not None else None
                if not isinstance(parent, ast.Attribute) or not isinstance(call, ast.Call):
                    return None, "only query_params.get and getlist are converted"
                if parent.attr == "get":
                    call.func = ast.Name(id="_query_get", ctx=ast.Load())
                elif parent.attr == "getlist":
                    item.attr = "args"
                else:
                    return None, "only query_params.get and getlist are converted"
        if (
            isinstance(item, ast.Call)
            and isinstance(item.func, ast.Name)
            and item.func.id == "Response"
            and (
                len(item.args) > 1 or any(k.arg not in {"status", "headers"} for k in item.keywords)
            )
        ):
            return None, "unsupported Response arguments"

    class JsonBody(ast.NodeTransformer):
        def visit_Attribute(self, item: ast.Attribute) -> ast.AST:
            if (
                isinstance(item.value, ast.Name)
                and item.value.id == "request"
                and item.attr == "data"
            ):
                return ast.Call(
                    func=ast.Name(id="_json_data", ctx=ast.Load()), args=[], keywords=[]
                )
            return self.generic_visit(item)

    node = cast(ast.FunctionDef, JsonBody().visit(node))
    node.body = imports + node.body
    return ast.unparse(ast.fix_missing_locations(node)), ""


def _scan(root: Path, config: dict[str, JsonValue]) -> dict[str, Any]:
    module = _settings(root, config)
    sys.path.insert(0, str(root))
    os.environ["DJANGO_SETTINGS_MODULE"] = module
    import django  # type: ignore[import-untyped]

    django.setup()
    from django.conf import settings
    from django.urls import URLResolver, get_resolver  # type: ignore[import-untyped]
    from django.urls.converters import IntConverter, StringConverter  # type: ignore[import-untyped]
    from django.urls.resolvers import RoutePattern  # type: ignore[import-untyped]

    routes: list[dict[str, Any]] = []

    def visit(patterns: Any, prefix: str = "", parent_supported: bool = True) -> None:
        for pattern in patterns:
            raw = prefix + str(pattern.pattern)
            supported = (
                parent_supported
                and isinstance(pattern.pattern, RoutePattern)
                and all(
                    type(converter) in {IntConverter, StringConverter}
                    for converter in pattern.pattern.converters.values()
                )
            )
            if isinstance(pattern, URLResolver):
                visit(pattern.url_patterns, raw, supported)
                continue
            callback = pattern.callback
            view = getattr(callback, "cls", None)
            actions = getattr(callback, "actions", None)
            methods = (
                list(actions)
                if actions
                else [m for m in getattr(view, "http_method_names", []) if hasattr(view, m)]
            )
            path = _flask_path(raw) if supported else None
            for method in methods or ["get"]:
                source, reason = (
                    _handler(view, method.upper()) if view else (None, "non-DRF callback")
                )
                if getattr(callback, "initkwargs", {}) or pattern.default_args:
                    source, reason = (
                        None,
                        "custom view initialization or URL defaults require manual migration",
                    )
                if settings.MIDDLEWARE:
                    source, reason = None, "source middleware requires manual migration"
                if not path:
                    source, reason = None, "URL pattern needs a custom Flask converter"
                routes.append(
                    {
                        "path": path,
                        "source_path": raw,
                        "method": method.upper(),
                        "source": source,
                        "classification": "native" if source else "needs_adaptation",
                        "reasons": [reason] if reason else [],
                        "view": f"{view.__module__}.{view.__name__}" if view else repr(callback),
                    }
                )

    visit(get_resolver().url_patterns)
    routes.sort(key=lambda route: (route["source_path"], route["method"]))
    return {"settings_module": module, "source_hash": _source_hash(root), "routes": routes}


def _render(scan: dict[str, Any]) -> dict[str, str]:
    pieces = [
        """# Generated by Sanka DRF-to-Flask. Repair the gaps listed in migration-gaps.json.
import os
import json
import datetime
import decimal
import uuid
os.environ["DJANGO_SETTINGS_MODULE"] = "sanka_flask_settings"
import django
django.setup()
from flask import Flask, g, jsonify, request
from flask.json.provider import DefaultJSONProvider
from django.db.models.query import QuerySet

class _OrmJSON(DefaultJSONProvider):
    @staticmethod
    def default(value):
        if isinstance(value, decimal.Decimal):
            return float(value)
        if isinstance(value, datetime.datetime):
            text = value.isoformat()
            return text[:-6] + "Z" if text.endswith("+00:00") else text
        if isinstance(value, datetime.time) and value.utcoffset() is not None:
            raise ValueError("JSON can't represent timezone-aware times.")
        if isinstance(value, (datetime.date, datetime.time)):
            return value.isoformat()
        if isinstance(value, datetime.timedelta):
            return str(value.total_seconds())
        if isinstance(value, uuid.UUID):
            return str(value)
        if isinstance(value, QuerySet):
            return list(value)
        return DefaultJSONProvider.default(value)

app = Flask(__name__)
app.json = _OrmJSON(app)
app.json.sort_keys = False

class _RequestError(Exception):
    def __init__(self, detail, status):
        self.detail, self.status = detail, status

@app.errorhandler(_RequestError)
def _request_error(error):
    return Response({"detail": error.detail}, status=error.status)

def _json_data():
    if hasattr(g, "_sanka_json_data"):
        return g._sanka_json_data
    raw = request.get_data()
    if not raw:
        g._sanka_json_data = {}
        return g._sanka_json_data
    if request.mimetype != "application/json":
        detail = 'Unsupported media type "' + (request.content_type or '') + '" in request.'
        raise _RequestError(detail, 415)
    def reject_constant(value):
        raise ValueError("Out of range float values are not JSON compliant: " + repr(value))
    try:
        decoded = raw.decode(request.mimetype_params.get("charset", "utf-8"))
        g._sanka_json_data = json.loads(decoded, parse_constant=reject_constant)
        return g._sanka_json_data
    except (ValueError, UnicodeError) as error:
        raise _RequestError("JSON parse error - " + str(error), 400) from error

def _query_get(key, default=None):
    values = request.args.getlist(key)
    return values[-1] if values else default

def Response(data=None, status=200, headers=None):
    response = jsonify(data) if data is not None else app.response_class()
    response.status_code = status
    if headers:
        response.headers.update(headers)
    return response
"""
    ]
    for index, route in enumerate(scan["routes"]):
        path = route["path"]
        if not path:
            continue
        name = f"route_{index}"
        if route["source"]:
            node = ast.parse(route["source"]).body[0]
            assert isinstance(node, ast.FunctionDef)
            node.name = name
            pieces.append(ast.unparse(node))
        else:
            pieces.append(
                f"def {name}(**kwargs):\n"
                '    return Response({"detail": "Migration required."}, status=501)'
            )
        pieces.append(
            f"app.add_url_rule({path!r}, {name!r}, {name}, "
            f"methods={[route['method']]!r}, provide_automatic_options=False)"
        )
        if route["method"] == "GET":
            pieces.append(
                f"for rule in app.url_map.iter_rules({name!r}):\n    rule.methods.discard('HEAD')"
            )
    settings = f"""from {scan["settings_module"]} import *
INSTALLED_APPS = [a for a in INSTALLED_APPS if not a.startswith("rest_framework")]
MIDDLEWARE = []
ROOT_URLCONF = __name__
urlpatterns = []
"""
    gaps = [r for r in scan["routes"] if r["classification"] != "native"]
    return {
        "target_app.py": "\n\n".join(pieces) + "\n",
        "sanka_flask_settings.py": settings,
        "migration-gaps.json": json.dumps(gaps, indent=2, sort_keys=True) + "\n",
        "requirements-flask.txt": "Flask>=3.1,<4\n"
        "# Install alongside the source Django ORM dependencies.\n",
    }


def _write_json(path: Path, data: dict[str, Any]) -> None:
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError("artifact path contains a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _apply(root: Path, output: Path, files: dict[str, str]) -> None:
    if output.exists():
        raise ValueError("output already exists; preserve repairs and choose a new output")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".flask-", dir=output.parent))
    try:
        for name, content in files.items():
            if Path(name).name != name or (root / name).exists():
                raise ValueError(f"generated file would replace existing source: {name}")
            if name.endswith(".py"):
                compile(content, name, "exec")
            (temporary / name).write_text(content)
        temporary.rename(output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def handle(request: ExtensionRequest) -> ExtensionResponse:
    try:
        root = Path(request.project_root).resolve()
        artifacts = Path(request.artifact_root)
        config = request.configuration
        if request.extension_id != "sanka/drf-to-flask":
            raise ValueError("extension identity does not match sanka/drf-to-flask")
        if request.command == "scan":
            with contextlib.redirect_stdout(io.StringIO()):
                data = _scan(root, config)
            artifact = artifacts / "scan.json"
            _write_json(artifact, data)
        elif request.command == "plan":
            if config.get("orm") not in {None, "django"}:
                raise ValueError("Flask currently retains the Django ORM")
            if (
                config.get("strategy", "native") != "native"
                or config.get("generation", "minimal") != "minimal"
            ):
                raise ValueError("Flask supports only native strategy and minimal generation")
            scan = json.loads((artifacts / "scan.json").read_text())
            if scan["source_hash"] != _source_hash(root):
                raise ValueError("source changed after scan; scan again")
            output = _within(root, str(config.get("output") or ".sanka/output/flask"))
            if output.exists():
                raise ValueError("output already exists; preserve repairs")
            eligible = len(scan["routes"])
            native = sum(r["classification"] == "native" for r in scan["routes"])
            data = {
                **scan,
                "target": "flask",
                "mode": "native",
                "output": str(output),
                "native_eligible_routes": eligible,
                "native_routes": native,
                "readiness": native / eligible if eligible else 0.0,
                "needs_adaptation_routes": eligible - native,
                "files": _render(scan),
            }
            data["plan_hash"] = _hash(data)
            artifact = artifacts / "plan-flask.json"
            _write_json(artifact, data)
        elif request.command == "apply":
            plan = json.loads((artifacts / "plan-flask.json").read_text())
            digest = plan.pop("plan_hash")
            if (
                not request.reviewed_plan_hash
                or config.get("extension_plan_hash") != digest
                or _hash(plan) != digest
            ):
                raise ValueError(
                    "apply requires the current reviewed core and extension plan hashes"
                )
            if plan["source_hash"] != _source_hash(root):
                raise ValueError("source changed after plan; scan and review again")
            minimum = config.get("min_readiness", 0)
            if (
                isinstance(minimum, bool)
                or not isinstance(minimum, int | float)
                or not 0 <= minimum <= 100
            ):
                raise ValueError("min_readiness must be a percentage between 0 and 100")
            if plan["readiness"] * 100 < minimum:
                raise ValueError("readiness is below min_readiness; inspect the plan's manual gaps")
            if config.get("gap_report_only") or config.get("force"):
                raise ValueError(
                    "inspect the plan for gaps; force and gap-report-only are not supported"
                )
            if config.get("orm") not in {None, "django"}:
                raise ValueError("the reviewed Flask plan retains the Django ORM")
            configured = config.get("output")
            if configured is not None and _within(root, str(configured)) != Path(plan["output"]):
                raise ValueError("output differs from the reviewed plan")
            output = _within(root, str(config.get("bench_candidate") or plan["output"]))
            if config.get("bench_candidate"):
                output = _within(root, str(output / "overlay"))
            _apply(root, output, plan["files"])
            data = {
                "output": str(output),
                "plan_hash": digest,
                "routes_generated": plan["native_routes"],
                "needs_adaptation_routes": plan["needs_adaptation_routes"],
                "mode": "native",
            }
            artifact = output
        else:
            return failure_response(
                request,
                code="SANKA_EXTENSION_UNSUPPORTED_COMMAND",
                message="Use independent source/candidate tests; Flask replay is not yet supported",
            )
        return success_response(
            request,
            data=cast(dict[str, JsonValue], data),
            artifacts=[str(artifact.resolve())],
            limitations=[
                "Only recognized JSON APIView handlers and isolated ORM dependencies "
                "are converted. "
                "Review migration-gaps.json and independently verify all behavior."
            ],
        )
    except Exception as error:
        return failure_response(
            request, code="SANKA_EXTENSION_EXECUTION_FAILED", message=str(error)
        )

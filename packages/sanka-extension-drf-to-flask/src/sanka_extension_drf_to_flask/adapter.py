# SPDX-License-Identifier: Apache-2.0
"""Conservative native Flask generation, without importing another extension.

ponytail: this first release converts stateless JSON APIView handlers; serializers,
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
    raw = raw.removeprefix("^").removesuffix("$").removesuffix(r"\Z")
    raw = re.sub(r"\(\?P<(\w+)>\[\^/\.\]\+\)", r"<\1>", raw)
    raw = re.sub(r"\(\?P<(\w+)>\[0-9\]\+\)", r"<int:\1>", raw)
    # Django str and Flask string converters have the same single-segment scope.
    raw = raw.replace("<str:", "<string:")
    if re.search(r"[\\^$?*+()\[\]{}|]", raw):
        return None
    converters = re.findall(r"<(?:(\w+):)?\w+>", raw)
    if any(c not in {"", "int", "string", "slug", "uuid", "path"} for c in converters):
        return None
    # Flask has no slug converter; accepting arbitrary strings would widen the source route.
    if "slug" in converters:
        return None
    return "/" + raw.lstrip("/")


def _handler(view: Any, method: str) -> tuple[str | None, str]:
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
    }
    if function.__globals__.get("Response") is not Response:
        return None, "Response must be the DRF response constructor"
    if any(name in function.__globals__ for name in builtins):
        return None, "shadowed builtins require manual migration"
    used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    if used - bound - builtins - {"request", "Response"}:
        return None, "handler depends on source globals, serializers or services"
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
            if item.attr not in {"query_params", "method"}:
                return None, f"request.{item.attr} semantics require manual migration"
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
    return ast.unparse(node), ""


def _scan(root: Path, config: dict[str, JsonValue]) -> dict[str, Any]:
    module = _settings(root, config)
    sys.path.insert(0, str(root))
    os.environ["DJANGO_SETTINGS_MODULE"] = module
    import django  # type: ignore[import-untyped]

    django.setup()
    from django.conf import settings  # type: ignore[import-untyped]
    from django.urls import URLResolver, get_resolver  # type: ignore[import-untyped]

    routes: list[dict[str, Any]] = []

    def visit(patterns: Any, prefix: str = "") -> None:
        for pattern in patterns:
            raw = prefix + str(pattern.pattern)
            if isinstance(pattern, URLResolver):
                visit(pattern.url_patterns, raw)
                continue
            callback = pattern.callback
            view = getattr(callback, "cls", None)
            actions = getattr(callback, "actions", None)
            methods = (
                list(actions)
                if actions
                else [m for m in getattr(view, "http_method_names", []) if hasattr(view, m)]
            )
            path = _flask_path(raw)
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
os.environ["DJANGO_SETTINGS_MODULE"] = "sanka_flask_settings"
import django
django.setup()
from flask import Flask, jsonify, request
app = Flask(__name__)
app.json.sort_keys = False

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
                "Only stateless JSON APIView handlers are converted. "
                "Review migration-gaps.json and independently verify all behavior."
            ],
        )
    except Exception as error:
        return failure_response(
            request, code="SANKA_EXTENSION_EXECUTION_FAILED", message=str(error)
        )

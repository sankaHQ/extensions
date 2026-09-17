# SPDX-License-Identifier: Apache-2.0
"""Static, fail-closed capture of qualified public JSON GET endpoints. Never imports source."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from .models import capture_models
from .queries import capture_read

SOURCES = ("drf", "fastapi", "flask")
TARGETS = ("fiber", "chi", "mux", "gin")
VERSION = "0.1.0a1"
PATH = re.compile(r"/[A-Za-z0-9_/-]*\Z")
IMPORTS = {
    "flask": {"flask": {"Flask", "jsonify"}},
    "fastapi": {"fastapi": {"FastAPI"}},
    "drf": {
        "django.urls": {"path"},
        "rest_framework.decorators": {
            "api_view",
            "authentication_classes",
            "permission_classes",
            "renderer_classes",
        },
        "rest_framework.permissions": {"AllowAny"},
        "rest_framework.renderers": {"JSONRenderer"},
        "rest_framework.response": {"Response"},
    },
}


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    )


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


def configuration(raw: dict[str, Any]) -> dict[str, str]:
    allowed = {
        "source_framework",
        "target_framework",
        "source_file",
        "database_layer",
        "extension_plan_hash",
        "database_dialect",
        "migration_tool",
        "schema_mode",
        "models_file",
    }
    if set(raw) - allowed:
        raise ValueError("unknown configuration fields: " + ", ".join(sorted(set(raw) - allowed)))
    result = {
        "source_framework": raw.get("source_framework", ""),
        "target_framework": raw.get("target_framework", "fiber"),
        "source_file": raw.get("source_file", "app.py"),
        "database_layer": raw.get("database_layer", "none"),
    }
    if any(type(value) is not str for value in result.values()):
        raise ValueError("configuration values must be strings")
    if result["source_framework"] not in SOURCES:
        raise ValueError("source_framework must be drf, fastapi or flask")
    if result["target_framework"] not in TARGETS:
        raise ValueError("target_framework must be fiber, chi, mux or gin")
    if result["database_layer"] not in {"none", "pgx"}:
        raise ValueError("database_layer must be none or pgx")
    database_keys = {"database_dialect", "migration_tool", "schema_mode", "models_file"}
    if result["database_layer"] == "none" and database_keys & raw.keys():
        raise ValueError("database options require database_layer=pgx")
    if result["database_layer"] == "pgx":
        for key, default in {
            "database_dialect": "postgresql",
            "migration_tool": "goose",
            "schema_mode": "empty",
            "models_file": "models.py",
        }.items():
            value = raw.get(key, default)
            if type(value) is not str or (key != "models_file" and value != default):
                raise ValueError(f"only {key}={default} is qualified")
            result[key] = value
        model_file = result["models_file"]
        if (
            Path(model_file).name != model_file
            or not model_file.endswith(".py")
            or model_file == result["source_file"]
        ):
            raise ValueError("models_file must be a distinct top-level Python filename")
    filename = result["source_file"]
    if Path(filename).name != filename or not filename.endswith(".py"):
        raise ValueError("source_file must be a top-level Python filename")
    return result


def _json_value(value: Any) -> None:
    if value is None or type(value) in {str, bool, int}:
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item)
        return
    raise ValueError(
        "only literal JSON strings, integers, booleans, lists and objects are qualified"
    )


def _payload(
    node: ast.FunctionDef | ast.AsyncFunctionDef, framework: str, models: list[dict[str, Any]]
) -> dict[str, Any]:
    if node.type_params:
        raise ValueError("generic functions require additional capture")
    if node.returns:
        raise ValueError("response validation requires additional capture")
    read = capture_read(node, framework, models)
    if read is not None:
        return {"read": read}
    args = node.args
    if (
        args.defaults
        or args.kw_defaults
        or args.vararg
        or args.kwarg
        or args.kwonlyargs
        or args.posonlyargs
    ):
        raise ValueError("request parameters and defaults require additional capture")
    expected = ["request"] if framework == "drf" else []
    if [arg.arg for arg in args.args] != expected:
        raise ValueError("request parameters require additional capture")
    if any(arg.annotation for arg in args.args):
        raise ValueError("type-driven response/request validation requires additional capture")
    if len(node.body) != 1 or not isinstance(node.body[0], ast.Return):
        raise ValueError("business logic and side effects are not qualified yet")
    value = node.body[0].value
    if framework in {"drf", "flask"}:
        name = "Response" if framework == "drf" else "jsonify"
        if not (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == name
            and len(value.args) == 1
            and not value.keywords
        ):
            raise ValueError(f"expected a literal {name}(payload) response")
        value = value.args[0]
    try:
        body = ast.literal_eval(value) if value is not None else None
    except (ValueError, TypeError) as error:
        raise ValueError("dynamic response expressions are not qualified") from error
    if not isinstance(body, dict):
        raise ValueError("only JSON object responses are qualified")
    _json_value(body)
    return {"body": body}


def capture(root: Path, config: dict[str, str]) -> dict[str, Any]:
    framework = config["source_framework"]
    filename = config["source_file"]
    path = root / filename
    if path.is_symlink() or not path.is_file():
        raise ValueError("source_file must be a regular non-symlink file")
    # Inspect all source files, excluding only known generated/tool environments.
    records: dict[str, str] = {}
    gaps: list[str] = []
    ignored = {".git", ".venv", ".sanka", "__pycache__"}
    total = 0
    for directory, names, filenames in os.walk(root, followlinks=False):
        names[:] = sorted(name for name in names if name not in ignored)
        if any((Path(directory) / name).is_symlink() for name in names):
            raise ValueError("source symlinks are unsupported")
        for name in sorted(filenames):
            source = Path(directory) / name
            relative = source.relative_to(root)
            if source.is_symlink() or not source.is_file():
                raise ValueError("only regular source files are supported")
            total += source.stat().st_size
            if total > 10_000_000 or len(records) >= 1000:
                raise ValueError("source exceeds experimental capture limits")
            content = source.read_bytes()
            records[relative.as_posix()] = hashlib.sha256(content).hexdigest()
            if (
                source.suffix == ".py"
                and source != path
                and source != root / config.get("models_file", "")
            ):
                gaps.append(f"{relative}: additional Python modules require whole-project capture")
    models = []
    allowed_imports = {key: set(value) for key, value in IMPORTS[framework].items()}
    if config["database_layer"] == "pgx":
        try:
            model_module = Path(config["models_file"]).stem
            if not model_module.isidentifier() or model_module in sys.stdlib_module_names | {
                "os",
                "sqlalchemy",
                "django",
                "rest_framework",
                "flask",
                "fastapi",
                "migration_source",
            }:
                raise ValueError("models_file conflicts with runtime imports")
            models = capture_models(root / config["models_file"], framework)
            if framework != "drf":
                allowed_imports.update(
                    {
                        "os": {"environ"},
                        "sqlalchemy": {"create_engine", "select"},
                        "sqlalchemy.orm": {"Session"},
                    }
                )
            if framework == "flask":
                allowed_imports["flask"].add("request")
            # Preserve symbol provenance: a model called Session/Response must not
            # satisfy the framework constructor or ORM session contract.
            reserved = {name for names in allowed_imports.values() for name in names}
            allowed_imports[model_module] = {model["name"] for model in models} - reserved
        except (ValueError, TypeError, OSError, SyntaxError) as error:
            gaps.append("models: " + str(error))
    tree = ast.parse(path.read_text(), filename=filename)
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    imports: set[str] = set()
    assignments: dict[str, ast.expr] = {}
    for node in tree.body:
        available = imports | assignments.keys() | functions.keys() | {"__name__"}
        if models and isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            available |= {"list", "dict", "row", "session", "str"}
            available |= {arg.arg for arg in node.args.args}
        unresolved = {
            item.id
            for item in ast.walk(node)
            if isinstance(item, ast.Name)
            and isinstance(item.ctx, ast.Load)
            and item.id not in available
        }
        if unresolved:
            gaps.append(f"line {node.lineno}: unresolved symbols: {', '.join(sorted(unresolved))}")
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in allowed_imports:
            for alias in node.names:
                if (
                    alias.asname
                    or alias.name not in allowed_imports[node.module]
                    or alias.name in imports | assignments.keys() | functions.keys()
                ):
                    gaps.append(f"line {node.lineno}: unsupported or duplicate import")
                imports.add(alias.name)
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            name = node.targets[0].id
            if name in assignments or name in imports or name in functions:
                gaps.append(f"line {node.lineno}: symbol reassignment")
            assignments[name] = node.value
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name in functions or node.name in imports or node.name in assignments:
                gaps.append(f"line {node.lineno}: symbol reassignment")
            functions[node.name] = node
        else:
            gaps.append(f"line {node.lineno}: unsupported {type(node).__name__}")
    routes: list[dict[str, Any]] = []
    try:
        engine = assignments.get("engine")
        if engine is not None:
            if framework == "drf" or not models or not {"create_engine", "environ"} <= imports:
                raise ValueError("unsupported database engine setup")
            expected_engine = ast.parse('create_engine(environ["DATABASE_URL"])', mode="eval").body
            if ast.dump(engine) != ast.dump(expected_engine):
                raise ValueError("engine must use create_engine(environ['DATABASE_URL'])")
        if framework == "drf":
            if set(assignments) != {"urlpatterns"} or "path" not in imports:
                raise ValueError(
                    "DRF requires literal urlpatterns and no other module configuration"
                )
            urls = assignments["urlpatterns"]
            if not isinstance(urls, ast.List):
                raise ValueError("urlpatterns must be a literal list")
            bindings = []
            for url in urls.elts:
                if not (
                    isinstance(url, ast.Call)
                    and isinstance(url.func, ast.Name)
                    and url.func.id == "path"
                    and len(url.args) == 2
                    and not url.keywords
                    and isinstance(url.args[1], ast.Name)
                ):
                    raise ValueError("only literal path(pattern, view) registrations are qualified")
                bindings.append(("/" + ast.literal_eval(url.args[0]), url.args[1].id))
        else:
            constructor = "Flask" if framework == "flask" else "FastAPI"
            app = assignments.get("app")
            if (
                set(assignments) != ({"app", "engine"} if engine is not None else {"app"})
                or constructor not in imports
                or not isinstance(app, ast.Call)
            ):
                raise ValueError("expected one unmodified app constructor")
            if not isinstance(app.func, ast.Name) or app.func.id != constructor or app.keywords:
                raise ValueError("custom app configuration requires additional capture")
            expected_args = "__name__" if framework == "flask" else ""
            if ",".join(ast.unparse(arg) for arg in app.args) != expected_args:
                raise ValueError("custom app arguments require additional capture")
            bindings = []
            for name, function in functions.items():
                if len(function.decorator_list) != 1:
                    raise ValueError(
                        "extra decorators, middleware and dependencies are not qualified"
                    )
                route = function.decorator_list[0]
                if not (
                    isinstance(route, ast.Call)
                    and isinstance(route.func, ast.Attribute)
                    and isinstance(route.func.value, ast.Name)
                    and route.func.value.id == "app"
                    and route.func.attr == "get"
                    and len(route.args) == 1
                    and not route.keywords
                ):
                    raise ValueError("only app.get(literal_path) is qualified")
                bindings.append((ast.literal_eval(route.args[0]), name))
        used: set[str] = set()
        for route_path, name in bindings:
            if type(route_path) is not str or not PATH.fullmatch(route_path) or "//" in route_path:
                raise ValueError("dynamic route parameters or nonliteral paths are not qualified")
            function = functions[name]
            used.add(name)
            if framework == "drf":
                decorators = [ast.unparse(item) for item in function.decorator_list]
                required = [
                    "api_view(['GET'])",
                    "authentication_classes([])",
                    "permission_classes([AllowAny])",
                    "renderer_classes([JSONRenderer])",
                ]
                if (
                    decorators != required
                    or not {
                        "api_view",
                        "authentication_classes",
                        "permission_classes",
                        "renderer_classes",
                        "AllowAny",
                        "JSONRenderer",
                        "Response",
                    }
                    <= imports
                ):
                    raise ValueError(
                        "DRF requires explicit GET, no auth, AllowAny and JSONRenderer decorators"
                    )
            elif framework == "flask" and "jsonify" not in imports:
                raise ValueError("jsonify must be imported from flask")
            if isinstance(function, ast.AsyncFunctionDef) and framework != "fastapi":
                raise ValueError("async handlers are qualified only for FastAPI")
            payload = _payload(function, framework, models)
            if (
                "read" in payload
                and framework != "drf"
                and (engine is None or not {"Session", "select"} <= imports)
            ):
                raise ValueError("database reads require an explicit engine, Session and select")
            routes.append(
                {
                    "path": route_path,
                    "method": "GET",
                    "status": 200,
                    **payload,
                }
            )
        if (
            any("read" in route for route in routes)
            and {"list", "dict", "row", "session", "str"} & functions.keys()
        ):
            raise ValueError("query symbols must not be shadowed")
        if engine is not None and not any("read" in route for route in routes):
            raise ValueError("unused database engine setup requires additional capture")
        if used != set(functions):
            raise ValueError("unregistered functions require additional capture")
        if len({route["path"] for route in routes}) != len(routes):
            raise ValueError("duplicate routes require ordering analysis")
    except (ValueError, TypeError, KeyError) as error:
        gaps.append(str(error))
    if not routes:
        gaps.append("no qualified endpoints")
    result = {
        "schema": "sanka.python-to-golang.capture/v1",
        "source_digest": digest(records),
        "configuration": config,
        "routes": sorted(routes, key=lambda item: item["path"]),
        "gaps": sorted(set(gaps)),
        "scope": "literal public JSON GET endpoints",
        "complete_backend": False,
    }

    if config["database_layer"] == "pgx":
        result["models"] = models
        result["scope"] = "empty PostgreSQL schema baseline and captured public JSON GET endpoints"
    return result

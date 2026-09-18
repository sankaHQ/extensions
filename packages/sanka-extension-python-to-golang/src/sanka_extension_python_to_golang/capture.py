# SPDX-License-Identifier: Apache-2.0
"""Static, fail-closed capture of qualified JSON endpoints. Never imports source."""

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
FLASK_INT_PATH = re.compile(r"(?P<prefix>/[A-Za-z0-9_/-]*)<int:(?P<name>[a-z][a-z0-9_]*)>\Z")
FASTAPI_INT_PATH = re.compile(r"(?P<prefix>/[A-Za-z0-9_/-]*)\{(?P<name>[a-z][a-z0-9_]*)\}\Z")
IMPORTS = {
    "flask": {"flask": {"Flask", "jsonify"}},
    "fastapi": {"fastapi": {"FastAPI", "HTTPException"}},
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
        "target",
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
    for key in ("target", "target_framework"):
        if key in raw and (type(raw[key]) is not str or raw[key] not in TARGETS):
            raise ValueError(f"{key} must be fiber, chi, mux or gin")
    if "target" in raw and "target_framework" in raw and raw["target"] != raw["target_framework"]:
        raise ValueError("target and target_framework must match")
    result = {
        "source_framework": raw.get("source_framework", ""),
        "target_framework": raw.get("target_framework", raw.get("target", "fiber")),
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


def _write_validation(fields: list[dict[str, Any]], data: str, partial: bool, error: str) -> str:
    writable = [field for field in fields if not field["auto"]]
    allowed = "{" + ", ".join(repr(field["name"]) for field in writable) + "}"
    conditions = [f"type({data}) is not dict", f"set({data}) - {allowed}"]
    for field in writable:
        name = repr(field["name"])
        value = f"{data}[{name}]"
        if not partial and not field["nullable"]:
            conditions.append(f"{name} not in {data}")
        if field["go_type"] in {"int32", "int64"}:
            bits = 32 if field["go_type"] == "int32" else 64
            lower, upper = -(2 ** (bits - 1)), 2 ** (bits - 1) - 1
            invalid = f"type({value}) is not int or not {lower} <= {value} <= {upper}"
        else:
            python_type = {"string": "str", "bool": "bool"}[field["go_type"]]
            invalid = f"type({value}) is not {python_type}"
        if field["nullable"]:
            invalid = f"{value} is not None and ({invalid})"
        if partial or field["nullable"]:
            invalid = f"{name} in {data} and ({invalid})"
        conditions.append(invalid)
    joined = "\n        or ".join(f"({condition})" for condition in conditions)
    return f"""if (
        {joined}
    ):
        {error}
"""


def _sqlalchemy_write(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    framework: str,
    method: str,
    models: list[dict[str, Any]],
) -> dict[str, Any]:
    if isinstance(node, ast.AsyncFunctionDef):
        raise ValueError("async database writes require additional capture")
    for model in models:
        fields = model["fields"]
        writable = [field for field in fields if not field["auto"]]
        response = (
            "{"
            + ", ".join(repr(field["name"]) + ": item." + field["name"] for field in fields)
            + "}"
        )
        if method == "POST":
            values = ", ".join(
                field["name"]
                + "="
                + (
                    f"data.get({field['name']!r})"
                    if field["nullable"]
                    else f"data[{field['name']!r}]"
                )
                for field in writable
            )
            prefix = "data = request.get_json()\n" if framework == "flask" else ""
            invalid = (
                'return jsonify({"error": "invalid request body"}), 400'
                if framework == "flask"
                else 'raise HTTPException(status_code=400, detail="invalid request body")'
            )
            validation = _write_validation(fields, "data", False, invalid)
            result = f"jsonify({response}), 201" if framework == "flask" else response
            source = (
                prefix
                + validation
                + f"""with Session(engine) as session:
    item = {model["name"]}({values})
    session.add(item)
    session.commit()
    session.refresh(item)
    return {result}
"""
            )
            expected_args: list[str] = [] if framework == "flask" else ["data"]
            expected_annotations = [""] * len(expected_args) if framework == "flask" else ["dict"]
            write = {"operation": "create", "model": model["name"]}
            status = 201
        elif method == "DELETE":
            primary = next((field for field in fields if field["primary_key"]), None)
            if primary is None:
                continue
            missing = (
                'return jsonify({"error": "not found"}), 404'
                if framework == "flask"
                else 'raise HTTPException(status_code=404, detail="not found")'
            )
            result = 'return "", 204' if framework == "flask" else ""
            source = f"""with Session(engine) as session:
    item = session.get({model["name"]}, {primary["name"]})
    if item is None:
        {missing}
    session.delete(item)
    session.commit()
    {result}
"""
            expected_args = [primary["name"]]
            expected_annotations = [""] if framework == "flask" else ["int"]
            write = {"operation": "delete", "model": model["name"], "lookup": primary["name"]}
            status = 204
        else:
            primary = next((field for field in fields if field["primary_key"]), None)
            if primary is None:
                continue
            assignments = "\n".join(
                f"""    if {field["name"]!r} in data:
        item.{field["name"]} = data[{field["name"]!r}]"""
                for field in writable
            )
            prefix = "data = request.get_json()\n" if framework == "flask" else ""
            invalid = (
                'return jsonify({"error": "invalid request body"}), 400'
                if framework == "flask"
                else 'raise HTTPException(status_code=400, detail="invalid request body")'
            )
            validation = _write_validation(fields, "data", True, invalid)
            missing = (
                'return jsonify({"error": "not found"}), 404'
                if framework == "flask"
                else 'raise HTTPException(status_code=404, detail="not found")'
            )
            result = f"jsonify({response})" if framework == "flask" else response
            source = (
                prefix
                + validation
                + f"""with Session(engine) as session:
    item = session.get({model["name"]}, {primary["name"]})
    if item is None:
        {missing}
{assignments}
    session.commit()
    session.refresh(item)
    return {result}
"""
            )
            expected_args = [primary["name"]] if framework == "flask" else [primary["name"], "data"]
            expected_annotations = (
                [""] * len(expected_args) if framework == "flask" else ["int", "dict"]
            )
            write = {"operation": "patch", "model": model["name"], "lookup": primary["name"]}
            status = 200
        if (
            [arg.arg for arg in node.args.args] == expected_args
            and [ast.unparse(arg.annotation) if arg.annotation else "" for arg in node.args.args]
            == expected_annotations
            and not node.args.defaults
            and not node.args.kw_defaults
            and not node.args.kwonlyargs
            and not node.args.posonlyargs
            and node.args.vararg is None
            and node.args.kwarg is None
            and ast.dump(ast.Module(body=node.body, type_ignores=[])) == ast.dump(ast.parse(source))
        ):
            return {"status": status, "write": write}
    raise ValueError(f"write handler is outside the qualified {framework} recipe")


def _drf_write(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    method: str,
    models: list[dict[str, Any]],
) -> dict[str, Any]:
    if isinstance(node, ast.AsyncFunctionDef):
        raise ValueError("async DRF writes require additional capture")
    for model in models:
        fields = model["fields"]
        writable = [field for field in fields if not field["auto"]]
        response = (
            "{"
            + ", ".join(repr(field["name"]) + ": item." + field["name"] for field in fields)
            + "}"
        )
        if method == "POST":
            values = ", ".join(
                field["name"]
                + "="
                + (
                    f"request.data.get({field['name']!r})"
                    if field["nullable"]
                    else f"request.data[{field['name']!r}]"
                )
                for field in writable
            )
            validation = _write_validation(
                fields,
                "request.data",
                False,
                'return Response({"error": "invalid request body"}, status=400)',
            )
            source = (
                validation
                + f"""item = {model["name"]}.objects.create({values})
return Response({response}, status=201)
"""
            )
            expected_args = ["request"]
            status = 201
            write = {"operation": "create", "model": model["name"]}
        elif method == "DELETE":
            primary = next((field for field in fields if field["primary_key"]), None)
            if primary is None:
                continue
            lookup = (
                f"item = {model['name']}.objects.filter("
                f"{primary['name']}={primary['name']}).first()\n"
            )
            source = (
                lookup
                + """if item is None:
    return Response({"error": "not found"}, status=404)
item.delete()
return Response(status=204)
"""
            )
            expected_args = ["request", primary["name"]]
            status = 204
            write = {"operation": "delete", "model": model["name"], "lookup": primary["name"]}
        else:
            primary = next((field for field in fields if field["primary_key"]), None)
            if primary is None:
                continue
            assignments = "\n".join(
                f"""if {field["name"]!r} in request.data:
    item.{field["name"]} = request.data[{field["name"]!r}]"""
                for field in writable
            )
            validation = _write_validation(
                fields,
                "request.data",
                True,
                'return Response({"error": "invalid request body"}, status=400)',
            )
            lookup = (
                f"item = {model['name']}.objects.filter("
                f"{primary['name']}={primary['name']}).first()\n"
            )
            source = (
                validation
                + lookup
                + f"""if item is None:
    return Response({{"error": "not found"}}, status=404)
{assignments}
item.save(update_fields=list(request.data))
return Response({response})
"""
            )
            expected_args = ["request", primary["name"]]
            status = 200
            write = {"operation": "patch", "model": model["name"], "lookup": primary["name"]}
        if (
            [arg.arg for arg in node.args.args] == expected_args
            and not any(arg.annotation for arg in node.args.args)
            and not node.args.defaults
            and not node.args.kw_defaults
            and not node.args.kwonlyargs
            and not node.args.posonlyargs
            and node.args.vararg is None
            and node.args.kwarg is None
            and ast.dump(ast.Module(body=node.body, type_ignores=[])) == ast.dump(ast.parse(source))
        ):
            return {"status": status, "write": write}
    raise ValueError("write handler is outside the qualified DRF recipe")


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
            available |= {
                "list",
                "bool",
                "dict",
                "int",
                "row",
                "session",
                "set",
                "str",
                "type",
                "data",
                "item",
            }
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
                bindings.append(("/" + ast.literal_eval(url.args[0]), url.args[1].id, ""))
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
                    and route.func.attr in {"get", "post", "patch", "delete"}
                    and len(route.args) == 1
                    and (
                        (
                            not route.keywords
                            and (framework == "flask" or route.func.attr in {"get", "patch"})
                        )
                        or (
                            framework == "fastapi"
                            and route.func.attr in {"post", "delete"}
                            and len(route.keywords) == 1
                            and route.keywords[0].arg == "status_code"
                            and ast.literal_eval(route.keywords[0].value)
                            == (201 if route.func.attr == "post" else 204)
                        )
                    )
                ):
                    raise ValueError("only app.get/post/patch/delete(literal_path) is qualified")
                bindings.append((ast.literal_eval(route.args[0]), name, route.func.attr.upper()))
        used: set[str] = set()
        for route_path, name, method in bindings:
            function = functions[name]
            if framework == "drf":
                first = ast.unparse(function.decorator_list[0]) if function.decorator_list else ""
                if first == "api_view(['GET'])":
                    method = "GET"
                elif first == "api_view(['POST'])":
                    method = "POST"
                elif first == "api_view(['PATCH'])":
                    method = "PATCH"
                elif first == "api_view(['DELETE'])":
                    method = "DELETE"
            match = FLASK_INT_PATH.fullmatch(route_path) if framework == "flask" else None
            if framework == "drf":
                match = FLASK_INT_PATH.fullmatch(route_path)
            elif framework == "fastapi":
                match = FASTAPI_INT_PATH.fullmatch(route_path)
            if match:
                route_path = match.group("prefix") + ":" + match.group("name")
            invalid_path = (
                type(route_path) is not str or not PATH.fullmatch(route_path) or "//" in route_path
            )
            if invalid_path and not (method in {"PATCH", "DELETE"} and ":" in route_path):
                raise ValueError("dynamic route parameters or nonliteral paths are not qualified")
            used.add(name)
            if framework == "drf":
                decorators = [ast.unparse(item) for item in function.decorator_list]
                required = [
                    f"api_view(['{method}'])",
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
            if method in {"POST", "PATCH", "DELETE"} and framework in {"flask", "fastapi"}:
                payload = _sqlalchemy_write(function, framework, method, models)
            elif method in {"POST", "PATCH", "DELETE"} and framework == "drf":
                payload = _drf_write(function, method, models)
            else:
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
                    "method": method,
                    "status": payload.pop("status", 200),
                    **payload,
                }
            )
        if (
            any("read" in route for route in routes)
            and {"list", "dict", "row", "session", "str"} & functions.keys()
        ):
            raise ValueError("query symbols must not be shadowed")
        if engine is not None and not any("read" in route or "write" in route for route in routes):
            raise ValueError("unused database engine setup requires additional capture")
        if used != set(functions):
            raise ValueError("unregistered functions require additional capture")
        if len({(route["method"], route["path"]) for route in routes}) != len(routes):
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
        result["scope"] = "empty PostgreSQL schema baseline and captured JSON endpoints"
    return result

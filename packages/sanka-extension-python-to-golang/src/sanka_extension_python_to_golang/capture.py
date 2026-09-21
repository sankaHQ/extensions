# SPDX-License-Identifier: Apache-2.0
"""Static, fail-closed capture of qualified JSON endpoints. Never imports source."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath
from textwrap import indent
from typing import Any

from .application import normalize_application
from .async_persistence import normalize_async_persistence
from .models import capture_models
from .persistence import capture_fastapi_persistence
from .queries import capture_read
from .routing import normalize_routes, project_tree
from .security import normalize_security
from .topology import capture_fastapi_topology

SOURCES = ("drf", "fastapi", "flask")
TARGETS = ("fiber", "chi", "mux", "gin")
VERSION = "0.1.0a2"
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_SOURCE_FILES = 20_000
GAP_PATH_SAMPLES = 8
PATH = re.compile(r"/[A-Za-z0-9_/-]*\Z")
FLASK_INT_PATH = re.compile(r"(?P<prefix>/[A-Za-z0-9_/-]*)<int:(?P<name>[a-z][a-z0-9_]*)>\Z")
FASTAPI_INT_PATH = re.compile(r"(?P<prefix>/[A-Za-z0-9_/-]*)\{(?P<name>[a-z][a-z0-9_]*)\}\Z")
IMPORTS = {
    "flask": {"flask": {"Flask", "jsonify"}},
    "fastapi": {"fastapi": {"FastAPI", "HTTPException"}},
    "drf": {
        "django.urls": {"path", "include"},
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


def _python_path(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or not value.endswith(".py")
    ):
        raise ValueError(f"{label} must be a canonical relative Python path")
    return value


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
            if type(value) is not str:
                raise ValueError(f"only {key}={default} is qualified")
            if key == "schema_mode" and value not in {"empty", "adopt-existing"}:
                raise ValueError("schema_mode must be empty or adopt-existing")
            if key not in {"models_file", "schema_mode"} and value != default:
                raise ValueError(f"only {key}={default} is qualified")
            result[key] = value
        model_file = _python_path(result["models_file"], "models_file")
        if model_file == result["source_file"]:
            raise ValueError("models_file must be distinct from source_file")
    _python_path(result["source_file"], "source_file")
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


def _normalize_native_pydantic(
    tree: ast.Module,
    models: list[dict[str, Any]],
    framework: str,
    validations: dict[str, dict[str, Any]],
) -> ast.Module:
    """Lower ordinary flat FastAPI body models without executing source code."""
    imports = [
        node for node in tree.body if isinstance(node, ast.ImportFrom) and node.module == "pydantic"
    ]
    if not imports:
        return tree
    if (
        len(imports) != 1
        or imports[0].level
        or {(alias.name, alias.asname) for alias in imports[0].names}
        != {("BaseModel", None), ("Field", None)}
    ):
        return tree
    if framework != "fastapi" or not models:
        raise ValueError("native Pydantic request models require a qualified FastAPI write")
    required_imports = {
        "fastapi.exceptions": [("RequestValidationError", None)],
        "fastapi.responses": [("JSONResponse", None)],
    }
    for module, expected_aliases in required_imports.items():
        found = [
            node for node in tree.body if isinstance(node, ast.ImportFrom) and node.module == module
        ]
        if (
            len(found) != 1
            or found[0].level
            or [(alias.name, alias.asname) for alias in found[0].names] != expected_aliases
        ):
            raise ValueError("native Pydantic validation handler imports must be explicit")
    if not any(
        isinstance(node, ast.ImportFrom)
        and node.module == "fastapi"
        and node.level == 0
        and ("Request", None) in [(alias.name, alias.asname) for alias in node.names]
        for node in tree.body
    ):
        raise ValueError("native Pydantic validation handler requires FastAPI Request")
    handler = ast.parse(
        "def invalid_request(request: Request, exc: RequestValidationError):\n"
        "    return JSONResponse(status_code=422, "
        "content={'detail': 'invalid request body'})\n"
    ).body[0]
    handlers = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and ast.dump(node) == ast.dump(handler)
    ]
    apps = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "app"
    ]
    expected_app = ast.parse(
        "app = FastAPI(exception_handlers={RequestValidationError: invalid_request})"
    ).body[0]
    if len(handlers) != 1 or len(apps) != 1 or ast.dump(apps[0]) != ast.dump(expected_app):
        raise ValueError(
            "native Pydantic models require the qualified stable validation error handler"
        )
    apps[0].value = ast.Call(func=ast.Name(id="FastAPI", ctx=ast.Load()), args=[], keywords=[])
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    matched: dict[str, tuple[list[dict[str, Any]], bool]] = {}
    for name, candidate in classes.items():
        if name in {"int", "str", "bool", "dict", "list", "set", "type", "len"}:
            raise ValueError("schema names must not shadow builtins")
        for model in models:
            fields = [field for field in model["fields"] if not field["auto"]]
            for partial in (False, True):
                lines = [f"class {name}(BaseModel):"]
                for field in fields:
                    kind = {
                        "string": "str",
                        "bool": "bool",
                        "int32": "int",
                        "int64": "int",
                    }[field["go_type"]]
                    if field["nullable"]:
                        kind += " | None"
                    if field["go_type"] in {"int32", "int64"}:
                        bits = 32 if field["go_type"] == "int32" else 64
                        low, high = -(2 ** (bits - 1)), 2 ** (bits - 1) - 1
                        default = (
                            f" = Field(default=None, ge={low}, le={high})"
                            if partial or field["nullable"]
                            else f" = Field(ge={low}, le={high})"
                        )
                    else:
                        default = " = None" if partial or field["nullable"] else ""
                    lines.append(f"    {field['name']}: {kind}{default}")
                if ast.dump(candidate) == ast.dump(ast.parse("\n".join(lines)).body[0]):
                    matched[name] = (fields, partial)
                    break
            if name in matched:
                break
        if name not in matched:
            raise ValueError(
                "native Pydantic request models must exactly match one flat database model"
            )
    used: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        schema_args = [
            arg
            for arg in node.args.args
            if arg.annotation is not None and ast.unparse(arg.annotation) in matched
        ]
        if not schema_args:
            continue
        if len(schema_args) != 1:
            raise ValueError("a handler must have exactly one native Pydantic request body")
        argument = schema_args[0]
        assert argument.annotation is not None
        schema = ast.unparse(argument.annotation)
        fields, partial = matched[schema]
        expected_statement = ast.parse(
            f"{argument.arg} = {argument.arg}.model_dump(exclude_unset=True)"
        ).body[0]
        if not node.body or ast.dump(node.body[0]) != ast.dump(expected_statement):
            raise ValueError("native Pydantic bodies must be dumped with exclude_unset=True")
        argument.annotation = ast.Name(id="dict", ctx=ast.Load())
        invalid = 'raise HTTPException(status_code=400, detail="invalid request body")'
        node.body[0] = ast.copy_location(
            ast.parse(_write_validation(fields, argument.arg, partial, invalid)).body[0],
            node.body[0],
        )
        validations[node.name] = {
            "validation": {
                "kind": "pydantic",
                "schema": schema,
                "partial": partial,
                "error": {"status": 422, "body": {"detail": "invalid request body"}},
            }
        }
        used.add(schema)
    if used != classes.keys():
        raise ValueError("unused native Pydantic request model")
    discarded_imports = {"fastapi.exceptions", "fastapi.responses"}
    body = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) or node in imports or node in handlers:
            continue
        if isinstance(node, ast.ImportFrom) and node.module in discarded_imports:
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "fastapi":
            node.names = [alias for alias in node.names if alias.name != "Request"]
            if not node.names:
                continue
        body.append(node)
    tree.body = body
    return ast.fix_missing_locations(tree)


def _normalize_pydantic(
    tree: ast.Module,
    models: list[dict[str, Any]],
    framework: str,
    validations: dict[str, dict[str, Any]],
) -> ast.Module:
    """Lower explicit strict schemas to the existing, qualified write validator."""
    schema_imports = {"BaseModel", "ConfigDict", "Field", "ValidationError"}
    imported: set[str] = set()
    classes: dict[str, ast.ClassDef] = {}
    names: set[str] = set()
    for node in tree.body:
        declared = []
        if isinstance(node, ast.ImportFrom):
            declared = [a.asname or a.name for a in node.names]
            if node.module == "pydantic":
                if node.level or any(a.asname or a.name not in schema_imports for a in node.names):
                    raise ValueError("only explicit Pydantic schema imports are qualified")
                imported.update(a.name for a in node.names)
        elif isinstance(node, ast.Assign):
            declared = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            declared = [node.name]
        if names.intersection(declared):
            raise ValueError("schema symbols must not be reassigned")
        names.update(declared)
        if isinstance(node, ast.ClassDef):
            if node.name in {"int", "str", "bool", "dict", "list", "set", "type", "len"}:
                raise ValueError("schema names must not shadow builtins")
            classes[node.name] = node
    if not imported:
        return tree
    if framework not in {"fastapi", "flask"} or not models:
        raise ValueError("strict Pydantic schemas require a qualified SQLAlchemy write")
    used: set[str] = set()
    available: set[str] = set()
    imported_so_far: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "pydantic":
            imported_so_far.update(a.name for a in node.names)
        if isinstance(node, ast.ClassDef):
            if not {"BaseModel", "ConfigDict", "Field"} <= imported_so_far:
                raise ValueError("schema imports must precede their declarations")
            available.add(node.name)
        if not isinstance(node, ast.FunctionDef):
            continue
        locals_ = {
            n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
        }
        locals_ |= {arg.arg for arg in node.args.args}
        # The existing recipe checker still validates every remaining statement,
        # argument, decorator and database operation after this replacement.
        for index, statement in enumerate(node.body):
            if not isinstance(statement, ast.Try):
                continue
            matched = False
            for name in sorted(available - locals_):
                for model in models:
                    for partial in (False, True):
                        lines = [
                            f"class {name}(BaseModel):",
                            "    model_config = ConfigDict(strict=True, extra='forbid')",
                        ]
                        for field in model["fields"]:
                            if field["auto"]:
                                continue
                            key = field["name"]
                            if key.startswith(("_", "model_")) or key in schema_imports | {
                                "str",
                                "int",
                                "bool",
                            }:
                                raise ValueError("schema field conflicts with Pydantic names")
                            kind = {
                                "string": "str",
                                "bool": "bool",
                                "int32": "int",
                                "int64": "int",
                            }[field["go_type"]]
                            if field["nullable"]:
                                kind += " | None"
                            options = ["default=None"] if partial or field["nullable"] else []
                            if field["go_type"] in {"int32", "int64"}:
                                bits = 32 if field["go_type"] == "int32" else 64
                                options += [f"ge={-(2 ** (bits - 1))}", f"le={2 ** (bits - 1) - 1}"]
                            lines.append(f"    {key}: {kind} = Field({', '.join(options)})")
                        expected = ast.parse("\n".join(lines)).body[0]
                        candidate = copy.deepcopy(classes[name])
                        constraints: dict[str, dict[str, int]] = {}
                        for declaration in candidate.body:
                            if not isinstance(declaration, ast.AnnAssign) or not isinstance(
                                declaration.target, ast.Name
                            ):
                                continue
                            field = next(
                                (f for f in model["fields"] if f["name"] == declaration.target.id),
                                None,
                            )
                            value = declaration.value
                            if (
                                field is None
                                or not isinstance(value, ast.Call)
                                or not isinstance(value.func, ast.Name)
                                or value.func.id != "Field"
                            ):
                                continue
                            retained = []
                            bounds: dict[str, int] = {}
                            for keyword in value.keywords:
                                key = keyword.arg
                                allowed = (
                                    {"min_length", "max_length"}
                                    if field["go_type"] == "string"
                                    else {"ge", "le"}
                                    if field["go_type"] in {"int32", "int64"}
                                    else set()
                                )
                                if key not in allowed:
                                    retained.append(keyword)
                                    continue
                                bound = ast.literal_eval(keyword.value)
                                if type(bound) is not int or key in bounds:
                                    raise ValueError(
                                        "schema bounds must be unique literal integers"
                                    )
                                bounds[key] = bound
                                if key in {"ge", "le"}:
                                    bits = 32 if field["go_type"] == "int32" else 64
                                    low, high = -(2 ** (bits - 1)), 2 ** (bits - 1) - 1
                                    if not low <= bound <= high:
                                        raise ValueError(
                                            "schema integer bounds exceed the database type"
                                        )
                                    retained.append(
                                        ast.keyword(
                                            arg=key,
                                            value=ast.parse(
                                                str(low if key == "ge" else high), mode="eval"
                                            ).body,
                                        )
                                    )
                                elif not 0 <= bound <= 9223372036854775807:
                                    raise ValueError(
                                        "schema length bounds must fit nonnegative int64"
                                    )
                            if bounds.get("min_length", 0) > bounds.get(
                                "max_length", 9223372036854775807
                            ) or bounds.get("ge", -9223372036854775808) > bounds.get(
                                "le", 9223372036854775807
                            ):
                                raise ValueError("schema bounds are reversed")
                            value.keywords = retained
                            if field["go_type"] in {"int32", "int64"}:
                                bits = 32 if field["go_type"] == "int32" else 64
                                bounds = {
                                    k: v
                                    for k, v in bounds.items()
                                    if v
                                    != (-(2 ** (bits - 1)) if k == "ge" else 2 ** (bits - 1) - 1)
                                }
                            if bounds:
                                constraints[field["name"]] = bounds
                        if ast.dump(candidate) != ast.dump(expected):
                            continue
                        error = (
                            'return jsonify({"error": "invalid request body"}), 400'
                            if framework == "flask"
                            else (
                                "raise HTTPException(status_code=400, "
                                'detail="invalid request body")'
                            )
                        )
                        recipe = ast.parse(
                            f"try:\n    data = {name}.model_validate(data)"
                            ".model_dump(exclude_unset=True)\n"
                            f"except ValidationError:\n    {error}\n"
                        ).body[0]
                        if (
                            ast.dump(statement) != ast.dump(recipe)
                            or "ValidationError" not in imported
                        ):
                            continue
                        node.body[index] = ast.copy_location(
                            ast.parse(
                                _write_validation(model["fields"], "data", partial, error)
                            ).body[0],
                            statement,
                        )
                        if constraints:
                            validations[node.name] = {"constraints": constraints}
                        used.add(name)
                        matched = True
                        break
                    if matched:
                        break
                if matched:
                    break
    if used != classes.keys():
        raise ValueError(
            "unused or unsupported Pydantic schema; coercion, aliases, hooks "
            "and nested fields require additional capture"
        )
    tree.body = [
        node
        for node in tree.body
        if not isinstance(node, ast.ClassDef)
        and not (isinstance(node, ast.ImportFrom) and node.module == "pydantic")
    ]
    return ast.fix_missing_locations(tree)


def _normalize_drf_serializers(
    tree: ast.Module,
    models: list[dict[str, Any]],
    validations: dict[str, dict[str, Any]],
) -> ast.Module:
    """Recognize qualified flat DRF serializers without executing source code."""
    schema_imports = {"BaseSerializer", "ValidationError"}
    imports: set[str] = set()
    schemas: dict[str, tuple[list[dict[str, Any]], str]] = {}
    used: set[str] = set()
    result: list[ast.stmt] = []

    class ValidatedData(ast.NodeTransformer):
        def visit_Name(self, node: ast.Name) -> ast.expr:
            if node.id == "data" and isinstance(node.ctx, ast.Load):
                return ast.copy_location(ast.parse("request.data", mode="eval").body, node)
            return node

    native_imports = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module == "rest_framework"
        and node.level == 0
        and [(alias.name, alias.asname) for alias in node.names] == [("serializers", None)]
    ]
    strict_imports = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "rest_framework.serializers"
    ]
    if not native_imports and not strict_imports:
        return tree
    if len(native_imports) + len(strict_imports) != 1:
        raise ValueError("use one explicit DRF serializer import style")
    native = bool(native_imports)
    for node in tree.body:
        if node in native_imports:
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "rest_framework.serializers":
            if node.level or any(a.asname or a.name not in schema_imports for a in node.names):
                raise ValueError(
                    "only explicit BaseSerializer and ValidationError imports are qualified"
                )
            imports.update(a.name for a in node.names)
            continue
        if isinstance(node, ast.ClassDef):
            if not native and imports != schema_imports:
                raise ValueError("serializer imports must precede their declarations")
            for model in models:
                fields = [field for field in model["fields"] if not field["auto"]]
                if native:
                    lines = [f"class {node.name}(serializers.Serializer):"]
                    for field in fields:
                        options = []
                        if field["nullable"]:
                            options.extend(["required=False", "allow_null=True"])
                        if field["go_type"] == "string":
                            match = re.fullmatch(r"varchar\((\d+)\)", field["sql_type"])
                            if match:
                                options.append(f"max_length={match.group(1)}")
                            options.extend(["allow_blank=True", "trim_whitespace=False"])
                            field_type = "CharField"
                        elif field["go_type"] in {"int32", "int64"}:
                            bits = 32 if field["go_type"] == "int32" else 64
                            options.extend(
                                [
                                    f"min_value={-(2 ** (bits - 1))}",
                                    f"max_value={2 ** (bits - 1) - 1}",
                                ]
                            )
                            field_type = "IntegerField"
                        elif field["go_type"] == "bool":
                            field_type = "BooleanField"
                        else:
                            break
                        lines.append(
                            f"    {field['name']} = serializers.{field_type}({', '.join(options)})"
                        )
                    else:
                        source = "\n".join(lines)
                        if ast.dump(node) == ast.dump(ast.parse(source).body[0]):
                            schemas[node.name] = (fields, "drf")
                            break
                    continue
                error = 'raise ValidationError("invalid request body")'
                source = (
                    f"class {node.name}(BaseSerializer):\n"
                    "    def to_internal_value(self, data):\n"
                    "        if self.partial:\n"
                    + indent(_write_validation(fields, "data", True, error), "            ")
                    + "        else:\n"
                    + indent(_write_validation(fields, "data", False, error), "            ")
                    + "        return data\n"
                )
                if ast.dump(node) == ast.dump(ast.parse(source).body[0]):
                    schemas[node.name] = (fields, "strict")
                    break
            else:
                raise ValueError("serializer is outside the qualified flat DRF recipe")
            continue
        if isinstance(node, ast.FunctionDef):
            locals_ = {
                n.id
                for n in ast.walk(node)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
            }
            locals_ |= {arg.arg for arg in node.args.args}
            error = 'return Response({"error": "invalid request body"}, status=400)'
            for name, (fields, kind) in schemas.items():
                if name in locals_:
                    continue
                matched = False
                for partial in (False, True):
                    prefix = ast.parse(
                        f"serializer = {name}(data=request.data, partial={partial})\n"
                        "if not serializer.is_valid():\n"
                        f"    {error}\n"
                        "data = serializer.validated_data\n"
                    ).body
                    if ast.dump(ast.Module(body=node.body[:3], type_ignores=[])) != ast.dump(
                        ast.Module(body=prefix, type_ignores=[])
                    ):
                        continue
                    remaining = ast.Module(body=node.body[3:], type_ignores=[])
                    ValidatedData().visit(remaining)
                    node.body = (
                        ast.parse(_write_validation(fields, "request.data", partial, error)).body
                        + remaining.body
                    )
                    used.add(name)
                    if kind == "drf":
                        validations[node.name] = {
                            "validation": {
                                "kind": "drf",
                                "schema": name,
                                "partial": partial,
                            }
                        }
                    matched = True
                    break
                if matched:
                    break
        result.append(node)
    if not schemas or used != schemas.keys():
        raise ValueError("unused serializer or unsupported serializer invocation")
    tree.body = result
    return ast.fix_missing_locations(tree)


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
        elif method == "PUT":
            primary = next((field for field in fields if field["primary_key"]), None)
            if primary is None:
                continue
            assignments = "\n".join(
                f"    item.{field['name']} = "
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
            status = 200
            write = {"operation": "replace", "model": model["name"], "lookup": primary["name"]}
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
        elif method == "PUT":
            primary = next((field for field in fields if field["primary_key"]), None)
            if primary is None:
                continue
            assignments = "\n".join(
                f"item.{field['name']} = "
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
item.save(update_fields={[field["name"] for field in writable]!r})
return Response({response})
"""
            )
            expected_args = ["request", primary["name"]]
            status = 200
            write = {"operation": "replace", "model": model["name"], "lookup": primary["name"]}
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
    modules: list[str] = []
    tree = ast.parse(path.read_text(), filename=filename)
    try:
        tree, modules = project_tree(root, filename, config.get("models_file", ""))
    except (ValueError, TypeError, SyntaxError) as error:
        gaps.append("project: " + str(error))
    ignored = {".git", ".venv", ".sanka", "__pycache__"}
    total = 0
    unconsumed: list[str] = []
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
            if total > MAX_SOURCE_BYTES or len(records) >= MAX_SOURCE_FILES:
                raise ValueError(
                    f"source exceeds capture limits ({MAX_SOURCE_FILES} files, "
                    f"{MAX_SOURCE_BYTES} bytes)"
                )
            content = source.read_bytes()
            records[relative.as_posix()] = hashlib.sha256(content).hexdigest()
            if (
                source.suffix == ".py"
                and source != path
                and relative.as_posix() not in modules
                and source != root / config.get("models_file", "")
            ):
                unconsumed.append(relative.as_posix())
    persistence = None
    if framework == "fastapi":
        try:
            persistence = capture_fastapi_persistence(root)
        except (OSError, SyntaxError, TypeError, ValueError) as error:
            gaps.append("persistence: " + str(error))
        if persistence is not None:
            consumed = set(persistence["files"])
            unconsumed = [name for name in unconsumed if name not in consumed]
    if unconsumed:
        samples = ", ".join(unconsumed[:GAP_PATH_SAMPLES])
        gaps.append(
            f"project: {len(unconsumed)} additional Python modules require semantic capture: "
            f"{samples}"
        )
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
    lowered_repositories: set[tuple[str | None, str]] = set()
    application: dict[str, Any] = {}
    security: dict[str, Any] = {}
    try:
        tree, application = normalize_application(tree, framework)
        tree, security = normalize_security(tree, framework)
        if framework == "fastapi" and models:
            tree, lowered_repositories = normalize_async_persistence(tree)
        tree = normalize_routes(tree, framework)
    except (ValueError, TypeError, SyntaxError) as error:
        gaps.append("routing: " + str(error))
    validations: dict[str, dict[str, Any]] = {}
    try:
        tree = _normalize_native_pydantic(tree, models, framework, validations)
        tree = _normalize_pydantic(tree, models, framework, validations)
        if framework == "drf":
            tree = _normalize_drf_serializers(tree, models, validations)
    except (ValueError, TypeError, SyntaxError) as error:
        gaps.append("validation: " + str(error))
    if persistence is not None:
        source_modules = {filename, *modules}
        remaining_repositories = [
            item
            for item in persistence["repositories"]
            if item["module"] not in source_modules
            or (item["owner"], item["name"]) not in lowered_repositories
        ]
        lowered_locations = {
            (item["module"], item["name"]) for item in persistence["repositories"]
        } - {(item["module"], item["name"]) for item in remaining_repositories}
        resolved_gaps = {
            f"{module}:{name}: conditional repository flow requires capture"
            for module, name in lowered_locations
        }
        persistence_gaps = [gap for gap in persistence["gaps"] if gap not in resolved_gaps]
        gaps.extend(f"persistence: {gap}" for gap in persistence_gaps)
        lowered_schemas = {
            value["validation"]["schema"]
            for value in validations.values()
            if value.get("validation", {}).get("kind") == "pydantic"
        }
        lowered_schema_files = {filename, *modules}
        lowered_schemas.update(
            model["name"]
            for model in persistence["pydantic_models"]
            if model["module"] in lowered_schema_files
        )
        lowered_models = {model["name"] for model in models}
        requires_lowering = bool(
            persistence["migrations"]
            or remaining_repositories
            or {model["name"] for model in persistence["pydantic_models"]} - lowered_schemas
            or {model["name"] for model in persistence["sqlalchemy_models"]} - lowered_models
        )
        if requires_lowering:
            gaps.append("persistence: captured contracts require Go lowering")
        elif not persistence_gaps:
            persistence = None
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    imports: set[str] = set()
    assignments: dict[str, ast.expr] = {}
    for node in tree.body:
        available = imports | assignments.keys() | functions.keys() | {"__name__"}
        if models and isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            available |= {
                "list",
                "len",
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
                    and route.func.attr in {"get", "post", "put", "patch", "delete"}
                    and len(route.args) == 1
                    and (
                        (
                            not route.keywords
                            and (framework == "flask" or route.func.attr in {"get", "put", "patch"})
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
                    raise ValueError(
                        "only app.get/post/put/patch/delete(literal_path) is qualified"
                    )
                bindings.append((ast.literal_eval(route.args[0]), name, route.func.attr.upper()))
        used: set[str] = set()
        for route_path, name, method in bindings:
            function = functions[name]
            if function.returns or function.type_params:
                raise ValueError(
                    "response validation and generic handlers require additional capture"
                )
            if framework == "drf":
                first = ast.unparse(function.decorator_list[0]) if function.decorator_list else ""
                if first == "api_view(['GET'])":
                    method = "GET"
                elif first == "api_view(['POST'])":
                    method = "POST"
                elif first == "api_view(['PATCH'])":
                    method = "PATCH"
                elif first == "api_view(['PUT'])":
                    method = "PUT"
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
            if invalid_path and not (
                method in {"GET", "PUT", "PATCH", "DELETE"} and ":" in route_path
            ):
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
            if method in {"POST", "PUT", "PATCH", "DELETE"} and framework in {"flask", "fastapi"}:
                payload = _sqlalchemy_write(function, framework, method, models)
            elif method in {"POST", "PUT", "PATCH", "DELETE"} and framework == "drf":
                payload = _drf_write(function, method, models)
            else:
                payload = _payload(function, framework, models)
            if name in validations:
                payload["write"].update(validations[name])
            lookup = payload.get("read", {}).get("lookup")
            path_lookup = route_path.rsplit(":", 1)[1] if ":" in route_path else None
            if (
                method == "GET"
                and lookup != path_lookup
                and (lookup is not None or path_lookup is not None)
            ):
                raise ValueError("detail read lookup must match the dynamic route parameter")
            if (
                "read" in payload
                and framework != "drf"
                and (
                    engine is None
                    or "Session" not in imports
                    or ("lookup" not in payload["read"] and "select" not in imports)
                )
            ):
                raise ValueError("database reads require an explicit engine and ORM imports")
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
    topology = capture_fastapi_topology(root, filename) if framework == "fastapi" else None
    if topology is not None:
        gaps.extend(f"topology: {gap}" for gap in topology["gaps"])
    result = {
        "schema": "sanka.python-to-golang.capture/v1",
        "source_digest": digest(records),
        "source_inventory": {
            "files": len(records),
            "python_files": sum(name.endswith(".py") for name in records),
            "bytes": total,
        },
        "configuration": config,
        "routes": sorted(routes, key=lambda item: item["path"]),
        "gaps": sorted(set(gaps)),
        "scope": "literal public JSON GET endpoints",
        "complete_backend": False,
    }

    if modules:
        result["source_modules"] = modules
    if security:
        result["security"] = security
    if application:
        result["application"] = application
    if topology is not None:
        result["fastapi_topology"] = topology
    if persistence is not None:
        result["fastapi_persistence"] = persistence
    if config["database_layer"] == "pgx":
        result["models"] = models
        result["scope"] = (
            f"{config['schema_mode']} PostgreSQL schema baseline and captured JSON endpoints"
        )
    return result

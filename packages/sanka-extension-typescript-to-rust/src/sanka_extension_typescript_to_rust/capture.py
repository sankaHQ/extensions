# SPDX-License-Identifier: Apache-2.0
"""Static, fail-closed capture of qualified Express JSON GET endpoints.

Source files are parsed through the vendored TypeScript syntax driver and never
imported or executed. Anything outside the recognized envelope is a gap.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from sanka_ts_capture import TYPESCRIPT_SHA256, TYPESCRIPT_VERSION, ParsedFile, parse_sources
from sanka_ts_capture import tree as t

from .queries import capture_read
from .schema import capture_interfaces, capture_tables, match_models
from .writes import OPERATIONS, idiom_shapes, match_idiom

SOURCES = ("express",)
TARGETS = ("axum",)
VERSION = "0.1.0a2"
DEFAULT_SOURCE_FILE = "src/app.ts"
DEFAULT_SCHEMA_FILE = "schema.sql"
DEFAULT_MODELS_FILE = "src/models.ts"
DATABASE_LAYERS = ("none", "sqlx")
DATABASE_OPTIONS = (
    "database_dialect",
    "migration_tool",
    "schema_mode",
    "schema_file",
    "models_file",
)
LAUNCHER_NAMES = ("server.ts", "index.ts")
PATH = re.compile(r"/[A-Za-z0-9_./-]*\Z")
PARAM_PATH = re.compile(r"/[A-Za-z0-9_./-]*/:([A-Za-z_][A-Za-z0-9_]*)\Z")
BODY_OPERATIONS = frozenset({"create", "update", "replace"})
SOURCE_SUFFIXES = frozenset({".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"})
IGNORED = frozenset({".git", ".sanka", "node_modules", "dist"})
# Declared replay scenarios describe verification, not the application, so adding or
# editing them after apply must not invalidate the reviewed plan.
SCENARIO_FILE = "sanka-verify.json"
MAX_SOURCE_BYTES = 10_000_000
MAX_SOURCE_FILES = 1000
EXPRESS_MAJORS = (4, 5)
BODYLESS_STATUSES = frozenset({204, 205, 304})
_MAJOR = re.compile(r"\A\s*[\^~]?\s*(?:>=\s*)?v?(\d+)(?:\.|\Z)")

Gap = Callable[[t.Node, str], None]


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
        *DATABASE_OPTIONS,
        "extension_plan_hash",
    }
    if set(raw) - allowed:
        raise ValueError("unknown configuration fields: " + ", ".join(sorted(set(raw) - allowed)))
    target = raw.get("target_framework", raw.get("target", "axum"))
    if "target" in raw and "target_framework" in raw and raw["target"] != raw["target_framework"]:
        raise ValueError("target and target_framework must agree")
    result = {
        "source_framework": raw.get("source_framework", "express"),
        "target_framework": target,
        "source_file": raw.get("source_file", DEFAULT_SOURCE_FILE),
        "database_layer": raw.get("database_layer", "none"),
    }
    if result["database_layer"] == "sqlx":
        result.update(
            database_dialect=raw.get("database_dialect", "postgresql"),
            migration_tool=raw.get("migration_tool", "sqlx"),
            schema_mode=raw.get("schema_mode", "empty"),
            schema_file=raw.get("schema_file", DEFAULT_SCHEMA_FILE),
            models_file=raw.get("models_file", DEFAULT_MODELS_FILE),
        )
    if any(type(value) is not str for value in result.values()):
        raise ValueError("configuration values must be strings")
    if result["source_framework"] not in SOURCES:
        raise ValueError("source_framework must be express")
    if result["target_framework"] not in TARGETS:
        raise ValueError("target_framework must be axum")
    if result["database_layer"] not in DATABASE_LAYERS:
        raise ValueError("database_layer must be none or sqlx")
    result["source_file"] = _relative_file(result["source_file"], ".ts", "source_file")
    if result["database_layer"] == "none":
        if any(key in raw for key in DATABASE_OPTIONS):
            raise ValueError("database options require database_layer: sqlx")
        return result
    if result["database_dialect"] != "postgresql":
        raise ValueError("database_dialect must be postgresql")
    if result["migration_tool"] != "sqlx":
        raise ValueError("migration_tool must be sqlx")
    if result["schema_mode"] != "empty":
        raise ValueError("schema_mode must be empty; existing schemas are not adopted")
    result["schema_file"] = _relative_file(result["schema_file"], ".sql", "schema_file")
    result["models_file"] = _relative_file(result["models_file"], ".ts", "models_file")
    if result["models_file"] == result["source_file"]:
        raise ValueError("models_file must differ from source_file")
    return result


def _relative_file(value: str, suffix: str, what: str) -> str:
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.suffix != suffix
        or value != pure.as_posix()
    ):
        raise ValueError(f"{what} must be a relative {suffix} path inside the project")
    return value


def capture(root: Path, config: dict[str, str]) -> dict[str, Any]:
    relative = config["source_file"]
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise ValueError("source_file must be a regular non-symlink file")
    launchers = tuple(
        (PurePosixPath(relative).parent / name).as_posix()
        for name in LAUNCHER_NAMES
        if (PurePosixPath(relative).parent / name).as_posix() != relative
    )
    records: dict[str, str] = {}
    gaps: list[str] = []
    modules: list[str] = []
    total = 0
    for directory, names, filenames in os.walk(root, followlinks=False):
        names[:] = sorted(name for name in names if name not in IGNORED)
        if any((Path(directory) / name).is_symlink() for name in names):
            raise ValueError("source symlinks are unsupported")
        for name in sorted(filenames):
            source = Path(directory) / name
            if source.is_symlink() or not source.is_file():
                raise ValueError("only regular source files are supported")
            rel = source.relative_to(root).as_posix()
            if rel == SCENARIO_FILE:
                continue
            total += source.stat().st_size
            if total > MAX_SOURCE_BYTES or len(records) >= MAX_SOURCE_FILES:
                raise ValueError("source exceeds experimental capture limits")
            records[rel] = hashlib.sha256(source.read_bytes()).hexdigest()
            if source.suffix.lower() in SOURCE_SUFFIXES:
                modules.append(rel)
    launcher: str | None = None
    models_file = config.get("models_file")
    for rel in sorted(modules):
        if rel in (relative, models_file):
            continue
        if rel in launchers and launcher is None:
            launcher = rel
            continue
        gaps.append(f"{rel}: additional modules require whole-project capture")
    package = _package(root, gaps)
    texts = {relative: path.read_text(encoding="utf-8")}
    if launcher is not None:
        texts[launcher] = (root / launcher).read_text(encoding="utf-8")
    if models_file is not None:
        models_path = root / models_file
        if models_path.is_file() and not models_path.is_symlink():
            texts[models_file] = models_path.read_text(encoding="utf-8")
        else:
            gaps.append(f"{models_file}: models_file is required for database_layer sqlx")
    parsed = parse_sources(texts)
    for rel, item in sorted(parsed.items()):
        for diagnostic in item.diagnostics:
            gaps.append(f"{rel}: syntax error {diagnostic.code}: {diagnostic.message}")
    routes: list[dict[str, Any]] = []
    models: list[dict[str, Any]] = []
    database = config["database_layer"] == "sqlx"
    if not any(item.diagnostics for item in parsed.values()):
        if models_file in parsed:
            models = _models(root, config, parsed[models_file], gaps)
        routes, module_gaps = _module(parsed[relative], texts[relative], relative, models, database)
        gaps.extend(module_gaps)
        if launcher is not None:
            gaps.extend(
                _launcher(parsed[launcher], texts[launcher], launcher, PurePosixPath(relative).stem)
            )
    if not routes:
        gaps.append("no qualified endpoints")
    routes.sort(key=lambda item: (str(item["path"]), str(item["method"])))
    result: dict[str, Any] = {
        "schema": "sanka.typescript-to-rust.capture/v1",
        "source_digest": digest(records),
        "configuration": config,
        "typescript": {"version": TYPESCRIPT_VERSION, "sha256": TYPESCRIPT_SHA256},
        "package": package,
        "launcher": launcher,
        "routes": routes,
        "gaps": sorted(set(gaps)),
        "scope": "literal public JSON GET endpoints",
        "complete_backend": False,
    }
    if database:
        result["models"] = models
        result["scope"] = "literal public JSON GET endpoints and bounded flat table reads"
        if any("write" in route or "lookup" in route for route in routes):
            result["scope"] += " and qualified single-table lookups and writes"
    return result


def _models(
    root: Path, config: dict[str, str], parsed: ParsedFile, gaps: list[str]
) -> list[dict[str, Any]]:
    try:
        tables = capture_tables(root / config["schema_file"])
        interfaces = capture_interfaces(parsed, config["models_file"])
        return match_models(tables, interfaces)
    except (ValueError, OSError, UnicodeDecodeError) as error:
        gaps.append(f"database: {error}")
        return []


def _package(root: Path, gaps: list[str]) -> dict[str, Any]:
    path = root / "package.json"
    if path.is_symlink() or not path.is_file():
        gaps.append("package.json: required to identify the Express version")
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        gaps.append("package.json: invalid JSON")
        return {}
    dependencies = data.get("dependencies") if isinstance(data, dict) else None
    spec = dependencies.get("express") if isinstance(dependencies, dict) else None
    if not isinstance(spec, str) or not spec:
        gaps.append("package.json: express must be declared in dependencies")
        return {}
    match = _MAJOR.match(spec)
    major = int(match.group(1)) if match else None
    if major not in EXPRESS_MAJORS:
        gaps.append(f"package.json: express {spec!r} is not a qualified major version (4 or 5)")
    return {"express": spec, "express_major": major}


def _module(
    parsed: ParsedFile,
    source: str,
    rel: str,
    models: list[dict[str, Any]],
    database: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    gaps: list[str] = []
    express_name: str | None = None
    app_name: str | None = None
    pool_class: str | None = None
    pool_name: str | None = None
    exported = False
    json_parser = False
    routes: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    def gap(node: t.Node, message: str) -> None:
        gaps.append(f"{rel}:{t.line_of(source, node)}: {message}")

    for statement in t.field_list(parsed.tree, "statements"):
        kind = t.kind(statement)
        if kind == "ImportDeclaration":
            clause = t.field(statement, "importClause")
            if clause is not None and clause.get("typeOnly"):
                continue
            if t.string_value(t.field(statement, "moduleSpecifier")) == "pg":
                if not database:
                    gap(statement, "pg requires database_layer: sqlx")
                elif pool_class is not None:
                    gap(statement, "duplicate pg import")
                else:
                    pool_class = _pg_import(statement, gap)
                continue
            name = _express_import(statement, gap)
            if name is not None:
                if express_name is not None:
                    gap(statement, "duplicate express import")
                express_name = name
        elif kind == "VariableStatement":
            if pool_class is not None and _declares_new(statement, pool_class):
                if pool_name is not None:
                    gap(statement, "duplicate pg Pool declaration")
                    continue
                pool_name = _pool_declaration(statement, pool_class)
                if pool_name is None:
                    gap(
                        statement,
                        "only const pool = new Pool({ connectionString: "
                        "process.env.DATABASE_URL }) is qualified",
                    )
                continue
            declared = _app_declaration(statement, express_name) if app_name is None else None
            if declared is None:
                gap(statement, "only one const app = express() declaration is qualified")
                continue
            app_name, exported = declared[0], declared[1] or exported
        elif kind == "ExpressionStatement":
            if app_name is None or express_name is None:
                gap(statement, "statements before the app declaration require additional capture")
                continue
            route = _registration(statement, app_name, express_name, gap, models, pool_name)
            if route is None:
                continue
            if route.get("middleware") == "json":
                json_parser = True
            elif "handler" in route:
                if route["operation"] in BODY_OPERATIONS and not json_parser:
                    gap(statement, "app.use(express.json()) must precede body-reading routes")
                    continue
                pending.append(route)
            else:
                routes.append(route)
        elif kind == "ExportAssignment":
            expression = t.field(statement, "expression")
            if (
                statement.get("exportEquals")
                or app_name is None
                or expression is None
                or t.identifier_name(t.unparenthesize(expression)) != app_name
            ):
                gap(statement, "only export default app is qualified")
                continue
            exported = True
        elif kind == "ExportDeclaration":
            if not _exports_app(statement, app_name):
                gap(statement, "only export { app } is qualified")
                continue
            exported = True
        elif kind in {"InterfaceDeclaration", "TypeAliasDeclaration", "EmptyStatement"}:
            continue
        else:
            gap(statement, f"unsupported {kind}")
    routes.extend(_resolve_idioms(pending, models, pool_name, gap))
    if express_name is None:
        gaps.append(f'{rel}: import express from "express" is required')
    if app_name is None:
        gaps.append(f"{rel}: const app = express() is required")
    if not exported:
        gaps.append(f"{rel}: the application must be exported (export default app)")
    if len({(route["path"], route["method"]) for route in routes}) != len(routes):
        gaps.append(f"{rel}: duplicate routes require ordering analysis")
    return routes, gaps


def _resolve_idioms(
    pending: list[dict[str, Any]],
    models: list[dict[str, Any]],
    pool_name: str | None,
    gap: Gap,
) -> list[dict[str, Any]]:
    """Compare lookup and write handlers with the canonical idioms, parsed once."""
    if not pending:
        return []
    routes: list[dict[str, Any]] = []
    if not models:
        for item in pending:
            gap(item["handler"], "lookups and writes require database_layer: sqlx with models")
        return routes
    shapes = idiom_shapes(models, {item["param"] for item in pending if item["param"]})
    for item in pending:
        try:
            payload = match_idiom(
                item["handler"],
                item["operation"],
                item["param"] or "id",
                models,
                shapes,
                item["names"],
                pool_name,
            )
        except ValueError as error:
            gap(item["handler"], str(error))
            continue
        routes.append({"path": item["path"], "method": item["method"], **payload})
    return routes


def _express_import(statement: t.Node, gap: Gap) -> str | None:
    specifier = t.string_value(t.field(statement, "moduleSpecifier"))
    clause = t.field(statement, "importClause")
    if specifier != "express":
        gap(statement, f"unsupported import {specifier!r}")
        return None
    if clause is None:
        gap(statement, "side-effect imports require additional capture")
        return None
    if clause.get("typeOnly"):
        return None
    bindings = t.field(clause, "namedBindings")
    if bindings is not None:
        if t.kind(bindings) != "NamedImports":
            gap(bindings, "namespace imports from express require additional capture")
        else:
            for element in t.field_list(bindings, "elements"):
                if not element.get("typeOnly"):
                    gap(element, "value imports from express require additional capture")
    name = t.identifier_name(t.field(clause, "name"))
    if name is None and bindings is None:
        gap(statement, "unsupported express import form")
    return name


def _pg_import(statement: t.Node, gap: Gap) -> str | None:
    clause = t.field(statement, "importClause")
    bindings = t.field(clause, "namedBindings") if clause is not None else None
    elements = t.field_list(bindings, "elements") if bindings is not None else []
    element = elements[0] if len(elements) == 1 else None
    local = t.identifier_name(t.field(element, "name")) if element is not None else None
    imported = t.identifier_name(t.field(element, "propertyName")) if element is not None else None
    if (
        clause is None
        or t.field(clause, "name") is not None
        or bindings is None
        or t.kind(bindings) != "NamedImports"
        or element is None
        or element.get("typeOnly")
        or local is None
        or (imported or local) != "Pool"
    ):
        gap(statement, 'only import { Pool } from "pg" is qualified')
        return None
    return local


def _declares_new(statement: t.Node, class_name: str) -> bool:
    declarations = t.field(statement, "declarationList")
    items = t.field_list(declarations, "declarations") if declarations is not None else []
    for item in items:
        initializer = t.field(item, "initializer")
        if initializer is None:
            continue
        initializer = t.unparenthesize(initializer)
        if t.kind(initializer) == "NewExpression" and (
            t.identifier_name(t.field(initializer, "expression")) == class_name
        ):
            return True
    return False


def _pool_declaration(statement: t.Node, class_name: str) -> str | None:
    """Name of ``const pool = new Pool({ connectionString: process.env.DATABASE_URL })``."""
    declarations = t.field(statement, "declarationList")
    items = t.field_list(declarations, "declarations") if declarations is not None else []
    if (
        t.modifier_kinds(statement)
        or declarations is None
        or declarations.get("decl") != "const"
        or len(items) != 1
    ):
        return None
    name = t.identifier_name(t.field(items[0], "name"))
    initializer = t.field(items[0], "initializer")
    if name is None or initializer is None or t.field(items[0], "type") is not None:
        return None
    initializer = t.unparenthesize(initializer)
    arguments = t.field_list(initializer, "arguments")
    if t.field_list(initializer, "typeArguments") or len(arguments) != 1:
        return None
    options = t.unparenthesize(arguments[0])
    properties = t.field_list(options, "properties")
    if t.kind(options) != "ObjectLiteralExpression" or len(properties) != 1:
        return None
    entry = properties[0]
    value = t.field(entry, "initializer")
    if (
        t.kind(entry) != "PropertyAssignment"
        or t.identifier_name(t.field(entry, "name")) != "connectionString"
        or value is None
        or t.property_chain(value) != ("process", "env", "DATABASE_URL")
    ):
        return None
    return name


def _app_declaration(statement: t.Node, express_name: str | None) -> tuple[str, bool] | None:
    modifiers = t.modifier_kinds(statement)
    if modifiers - {"ExportKeyword"} or express_name is None:
        return None
    declarations = t.field(statement, "declarationList")
    if declarations is None or declarations.get("decl") != "const":
        return None
    items = t.field_list(declarations, "declarations")
    if len(items) != 1:
        return None
    declaration = items[0]
    name = t.identifier_name(t.field(declaration, "name"))
    initializer = t.field(declaration, "initializer")
    if name is None or initializer is None or t.field(declaration, "type") is not None:
        return None
    parts = t.call_parts(initializer)
    if parts is None or parts[1]:
        return None
    if t.identifier_name(t.unparenthesize(parts[0])) != express_name:
        return None
    return name, "ExportKeyword" in modifiers


def _is_call(node: t.Node, chain: tuple[str, ...], argument_count: int) -> bool:
    parts = t.call_parts(node)
    return (
        parts is not None
        and t.property_chain(parts[0]) == chain
        and len(parts[1]) == argument_count
    )


def _registration(
    statement: t.Node,
    app_name: str,
    express_name: str,
    gap: Gap,
    models: list[dict[str, Any]],
    pool_name: str | None,
) -> dict[str, Any] | None:
    expression = t.field(statement, "expression")
    parts = t.call_parts(expression) if expression is not None else None
    chain = t.property_chain(parts[0]) if parts is not None else None
    if parts is None or chain is None or len(chain) != 2 or chain[0] != app_name:
        gap(statement, "only app.get(path, handler) and app.use(express.json()) are qualified")
        return None
    method, arguments = chain[1], parts[1]
    if method == "use":
        if len(arguments) != 1 or not _is_call(arguments[0], (express_name, "json"), 0):
            gap(statement, "middleware other than express.json() requires additional capture")
            return None
        return {"middleware": "json"}
    if method.upper() not in OPERATIONS:
        gap(statement, f"{method} routes require additional capture")
        return None
    if len(arguments) != 2:
        gap(statement, f"app.{method} requires a literal path and one inline handler")
        return None
    route_path = t.string_value(t.unparenthesize(arguments[0]))
    if route_path is None or "//" in route_path:
        gap(arguments[0], "nonliteral paths are not qualified")
        return None
    handler = t.unparenthesize(arguments[1])
    param_match = PARAM_PATH.fullmatch(route_path)
    if param_match is None:
        if not PATH.fullmatch(route_path):
            gap(arguments[0], "only literal paths with at most one trailing :param are qualified")
            return None
        if method == "get":
            try:
                payload = _handler(handler, models, pool_name)
            except ValueError as error:
                gap(arguments[1], str(error))
                return None
            return {"path": route_path, "method": "GET", **payload}
        if method != "post":
            gap(arguments[0], f"{method} routes need a trailing :param segment")
            return None
        operation, param = "create", ""
    else:
        if method == "post":
            gap(arguments[0], "post routes with parameters require additional capture")
            return None
        operation, param = OPERATIONS[method.upper()], param_match.group(1)
    try:
        names = _parameters(handler)
    except ValueError as error:
        gap(arguments[1], str(error))
        return None
    return {
        "path": route_path,
        "method": method.upper(),
        "operation": operation,
        "param": param,
        "handler": handler,
        "names": names,
    }


def _exports_app(statement: t.Node, app_name: str | None) -> bool:
    clause = t.field(statement, "exportClause")
    if (
        app_name is None
        or statement.get("typeOnly")
        or t.field(statement, "moduleSpecifier") is not None
        or clause is None
        or t.kind(clause) != "NamedExports"
    ):
        return False
    elements = t.field_list(clause, "elements")
    if len(elements) != 1 or elements[0].get("typeOnly"):
        return False
    element = elements[0]
    exported = t.identifier_name(t.field(element, "name"))
    local = t.identifier_name(t.field(element, "propertyName")) or exported
    return exported == app_name and local == app_name


def _parameters(node: t.Node) -> tuple[str, str]:
    """The (request, response) parameter names of an inline handler function."""
    if t.kind(node) not in {"ArrowFunction", "FunctionExpression"}:
        raise ValueError("only inline handler functions are qualified")
    if t.modifier_kinds(node) - {"AsyncKeyword"}:
        raise ValueError("unsupported handler modifiers")
    if t.field_list(node, "typeParameters") or t.field(node, "asteriskToken") is not None:
        raise ValueError("generic or generator handlers are not qualified")
    parameters = t.field_list(node, "parameters")
    if len(parameters) != 2:
        raise ValueError(
            "handlers must declare exactly (req, res); next and error handlers "
            "require additional capture"
        )
    names: list[str] = []
    for parameter in parameters:
        name = t.identifier_name(t.field(parameter, "name"))
        if (
            name is None
            or t.field(parameter, "initializer") is not None
            or t.field(parameter, "dotDotDotToken") is not None
        ):
            raise ValueError("destructured, default or rest parameters require additional capture")
        names.append(name)
    return names[0], names[1]


def _handler(node: t.Node, models: list[dict[str, Any]], pool_name: str | None) -> dict[str, Any]:
    node = t.unparenthesize(node)
    response = _parameters(node)[1]
    body = t.field(node, "body")
    if body is None:
        raise ValueError("handler body is required")
    if t.kind(body) == "Block":
        statements = t.field_list(body, "statements")
        if len(statements) == 2 and t.kind(statements[0]) == "VariableStatement":
            if not models:
                raise ValueError("database reads require database_layer: sqlx with captured models")
            return {"status": 200, "read": capture_read(statements, response, pool_name, models)}
        if len(statements) != 1 or t.kind(statements[0]) not in {
            "ExpressionStatement",
            "ReturnStatement",
        }:
            raise ValueError("business logic and side effects are not qualified yet")
        call = t.field(statements[0], "expression")
        if call is None:
            raise ValueError("expected a res.json(payload) response")
    else:
        call = body
    parts = t.call_parts(call)
    if parts is None:
        raise ValueError("expected a res.json(payload) response")
    callee = t.unparenthesize(parts[0])
    if (
        t.kind(callee) != "PropertyAccessExpression"
        or t.identifier_name(t.field(callee, "name")) != "json"
        or t.field(callee, "questionDotToken") is not None
        or len(parts[1]) != 1
    ):
        raise ValueError("expected a res.json(payload) response")
    receiver = t.field(callee, "expression")
    receiver = t.unparenthesize(receiver) if receiver is not None else {}
    status = 200
    if t.identifier_name(receiver) != response:
        status = _status_call(receiver, response)
    payload = t.unparenthesize(parts[1][0])
    encoded = payload.get("json")
    if t.kind(payload) != "ObjectLiteralExpression" or not isinstance(encoded, str):
        raise ValueError("only literal JSON object responses are qualified")
    return {"status": status, "body": json.loads(encoded), "body_json": encoded}


def _status_call(receiver: t.Node, response: str) -> int:
    parts = t.call_parts(receiver)
    callee = t.unparenthesize(parts[0]) if parts is not None else None
    if (
        parts is None
        or callee is None
        or t.kind(callee) != "PropertyAccessExpression"
        or t.identifier_name(t.field(callee, "name")) != "status"
        or t.field(callee, "questionDotToken") is not None
        or len(parts[1]) != 1
    ):
        raise ValueError("expected res.json(payload) or res.status(code).json(payload)")
    owner = t.field(callee, "expression")
    if owner is None or t.identifier_name(t.unparenthesize(owner)) != response:
        raise ValueError("expected res.json(payload) or res.status(code).json(payload)")
    code = t.unparenthesize(parts[1][0])
    text = t.text(code) if t.kind(code) == "NumericLiteral" else None
    if text is None or not text.isdigit() or not 200 <= int(text) <= 599:
        raise ValueError("status codes must be literal integers between 200 and 599")
    if int(text) in BODYLESS_STATUSES:
        raise ValueError("statuses that drop the response body are not qualified")
    return int(text)


def _launcher(parsed: ParsedFile, source: str, rel: str, stem: str) -> list[str]:
    gaps: list[str] = []
    imported: str | None = None
    listened = False
    specifiers = {f"./{stem}", f"./{stem}.js", f"./{stem}.ts"}
    for statement in t.field_list(parsed.tree, "statements"):
        kind = t.kind(statement)
        if kind == "ImportDeclaration" and imported is None:
            clause = t.field(statement, "importClause")
            name = t.identifier_name(t.field(clause, "name")) if clause is not None else None
            if (
                t.string_value(t.field(statement, "moduleSpecifier")) in specifiers
                and clause is not None
                and name is not None
                and t.field(clause, "namedBindings") is None
                and not clause.get("typeOnly")
            ):
                imported = name
                continue
        elif kind == "ExpressionStatement" and imported is not None and not listened:
            expression = t.field(statement, "expression")
            parts = t.call_parts(expression) if expression is not None else None
            if parts is not None and t.property_chain(parts[0]) == (imported, "listen"):
                listened = True
                continue
        gaps.append(
            f"{rel}:{t.line_of(source, statement)}: launcher statements other than importing "
            "the application and app.listen(...) require additional capture"
        )
    if not listened:
        gaps.append(f"{rel}: launcher must import the application and call app.listen(...)")
    return gaps

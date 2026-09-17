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

SOURCES = ("express",)
TARGETS = ("axum",)
VERSION = "0.1.0a1"
DEFAULT_SOURCE_FILE = "src/app.ts"
LAUNCHER_NAMES = ("server.ts", "index.ts")
PATH = re.compile(r"/[A-Za-z0-9_./-]*\Z")
SOURCE_SUFFIXES = frozenset({".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"})
IGNORED = frozenset({".git", ".sanka", "node_modules", "dist"})
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
    }
    if any(type(value) is not str for value in result.values()):
        raise ValueError("configuration values must be strings")
    if result["source_framework"] not in SOURCES:
        raise ValueError("source_framework must be express")
    if result["target_framework"] not in TARGETS:
        raise ValueError("target_framework must be axum")
    result["source_file"] = _source_file(result["source_file"])
    return result


def _source_file(value: str) -> str:
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.suffix != ".ts"
        or value != pure.as_posix()
    ):
        raise ValueError("source_file must be a relative .ts path inside the project")
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
            total += source.stat().st_size
            if total > MAX_SOURCE_BYTES or len(records) >= MAX_SOURCE_FILES:
                raise ValueError("source exceeds experimental capture limits")
            records[rel] = hashlib.sha256(source.read_bytes()).hexdigest()
            if source.suffix.lower() in SOURCE_SUFFIXES:
                modules.append(rel)
    launcher: str | None = None
    for rel in sorted(modules):
        if rel == relative:
            continue
        if rel in launchers and launcher is None:
            launcher = rel
            continue
        gaps.append(f"{rel}: additional modules require whole-project capture")
    package = _package(root, gaps)
    texts = {relative: path.read_text(encoding="utf-8")}
    if launcher is not None:
        texts[launcher] = (root / launcher).read_text(encoding="utf-8")
    parsed = parse_sources(texts)
    for rel, item in sorted(parsed.items()):
        for diagnostic in item.diagnostics:
            gaps.append(f"{rel}: syntax error {diagnostic.code}: {diagnostic.message}")
    routes: list[dict[str, Any]] = []
    if not any(item.diagnostics for item in parsed.values()):
        routes, module_gaps = _module(parsed[relative], texts[relative], relative)
        gaps.extend(module_gaps)
        if launcher is not None:
            gaps.extend(
                _launcher(parsed[launcher], texts[launcher], launcher, PurePosixPath(relative).stem)
            )
    if not routes:
        gaps.append("no qualified endpoints")
    return {
        "schema": "sanka.typescript-to-rust.capture/v1",
        "source_digest": digest(records),
        "configuration": config,
        "typescript": {"version": TYPESCRIPT_VERSION, "sha256": TYPESCRIPT_SHA256},
        "package": package,
        "launcher": launcher,
        "routes": sorted(routes, key=lambda item: str(item["path"])),
        "gaps": sorted(set(gaps)),
        "scope": "literal public JSON GET endpoints",
        "complete_backend": False,
    }


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


def _module(parsed: ParsedFile, source: str, rel: str) -> tuple[list[dict[str, Any]], list[str]]:
    gaps: list[str] = []
    express_name: str | None = None
    app_name: str | None = None
    exported = False
    routes: list[dict[str, Any]] = []

    def gap(node: t.Node, message: str) -> None:
        gaps.append(f"{rel}:{t.line_of(source, node)}: {message}")

    for statement in t.field_list(parsed.tree, "statements"):
        kind = t.kind(statement)
        if kind == "ImportDeclaration":
            name = _express_import(statement, gap)
            if name is not None:
                if express_name is not None:
                    gap(statement, "duplicate express import")
                express_name = name
        elif kind == "VariableStatement":
            declared = _app_declaration(statement, express_name) if app_name is None else None
            if declared is None:
                gap(statement, "only one const app = express() declaration is qualified")
                continue
            app_name, exported = declared[0], declared[1] or exported
        elif kind == "ExpressionStatement":
            if app_name is None or express_name is None:
                gap(statement, "statements before the app declaration require additional capture")
                continue
            route = _registration(statement, app_name, express_name, gap)
            if route is not None:
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
    if express_name is None:
        gaps.append(f'{rel}: import express from "express" is required')
    if app_name is None:
        gaps.append(f"{rel}: const app = express() is required")
    if not exported:
        gaps.append(f"{rel}: the application must be exported (export default app)")
    if len({route["path"] for route in routes}) != len(routes):
        gaps.append(f"{rel}: duplicate routes require ordering analysis")
    return routes, gaps


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
    statement: t.Node, app_name: str, express_name: str, gap: Gap
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
    if method != "get":
        gap(statement, f"{method} routes require additional capture")
        return None
    if len(arguments) != 2:
        gap(statement, "app.get requires a literal path and one inline handler")
        return None
    route_path = t.string_value(t.unparenthesize(arguments[0]))
    if route_path is None or not PATH.fullmatch(route_path) or "//" in route_path:
        gap(arguments[0], "dynamic route parameters or nonliteral paths are not qualified")
        return None
    try:
        payload = _handler(arguments[1])
    except ValueError as error:
        gap(arguments[1], str(error))
        return None
    return {"path": route_path, "method": "GET", **payload}


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


def _handler(node: t.Node) -> dict[str, Any]:
    node = t.unparenthesize(node)
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
    response = names[1]
    body = t.field(node, "body")
    if body is None:
        raise ValueError("handler body is required")
    if t.kind(body) == "Block":
        statements = t.field_list(body, "statements")
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

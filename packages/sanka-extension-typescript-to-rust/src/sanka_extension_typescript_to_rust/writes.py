# SPDX-License-Identifier: Apache-2.0
"""Qualified Express lookup and write idioms, recognized by exact syntax shape.

The canonical handler for each operation is rendered from the captured model,
parsed with the same TypeScript driver, and compared with the source handler
after erasing positions, parentheses and type annotations. This mirrors the
Python-to-Go extension, where the qualified write is the rendered idiom itself:
anything that deviates is a gap, never an approximation.
"""

from __future__ import annotations

import json
import re
from typing import Any

from sanka_ts_capture import parse_sources
from sanka_ts_capture import tree as t

from .queries import normalize_sql

OPERATIONS = {
    "GET": "lookup",
    "POST": "create",
    "PATCH": "update",
    "PUT": "replace",
    "DELETE": "delete",
}
STATUSES = {"lookup": 200, "create": 201, "update": 200, "replace": 200, "delete": 204}
WRITABLE_TYPES = frozenset({"i32", "bool", "String"})
ERASED_KINDS = frozenset(
    {
        "ParenthesizedExpression",
        "AsExpression",
        "TypeAssertionExpression",
        "NonNullExpression",
        "SatisfiesExpression",
    }
)
ERASED_FIELDS = frozenset({"type", "typeArguments", "typeParameters"})
REQUEST, RESPONSE, POOL = "sanka_req", "sanka_res", "sanka_pool"
INVALID = f'return {RESPONSE}.status(400).json({{ error: "invalid request body" }});'
MISSING = f'return {RESPONSE}.status(404).json({{ error: "not found" }});'
CONFLICT = f'return {RESPONSE}.status(409).json({{ error: "conflict" }});'
_SQL_START = re.compile(r"\A\s*(select|insert|update|delete)\b", re.IGNORECASE)


def shape(node: t.Node, rename: dict[str, str]) -> Any:
    """Position-free, type-erased structure used for exact idiom comparison."""
    while t.kind(node) in ERASED_KINDS:
        inner = t.field(node, "expression")
        if inner is None:
            break
        node = inner
    text = t.text(node)
    kind = t.kind(node)
    if text is not None:
        if kind == "Identifier":
            text = rename.get(text, text)
        elif kind in {"StringLiteral", "NoSubstitutionTemplateLiteral"} and _SQL_START.match(text):
            text = normalize_sql(text)
    children: list[tuple[str, Any]] = []
    for name, value in sorted(t.fields(node).items()):
        if name in ERASED_FIELDS:
            continue
        if isinstance(value, dict):
            children.append((name, shape(value, rename)))
        elif isinstance(value, list):
            children.append(
                (name, tuple(shape(item, rename) for item in value if isinstance(item, dict)))
            )
    return (
        kind,
        text,
        node.get("op"),
        node.get("decl"),
        bool(node.get("typeOnly")),
        tuple(children),
    )


def handler_shape(node: t.Node, rename: dict[str, str]) -> Any:
    """Parameters and body of a handler function, independent of arrow/function syntax."""
    parameters = tuple(shape(item, rename) for item in t.field_list(node, "parameters"))
    body = t.field(node, "body")
    return (parameters, shape(body, rename) if body is not None else None)


def writable(model: dict[str, Any]) -> list[dict[str, Any]]:
    return [field for field in model["fields"] if not field["auto"]]


def primary(model: dict[str, Any]) -> dict[str, Any]:
    return next(field for field in model["fields"] if field["primary_key"])


def qualified(model: dict[str, Any]) -> bool:
    """Only integer, boolean and text-like request fields have a qualified validation."""
    return all(field["rust_type"] in WRITABLE_TYPES for field in writable(model))


def columns(model: dict[str, Any]) -> str:
    return ", ".join(field["name"] for field in model["fields"])


def select_sql(model: dict[str, Any]) -> str:
    return f"SELECT {columns(model)} FROM {model['table']} WHERE {primary(model)['name']} = $1"


def insert_sql(model: dict[str, Any]) -> str:
    fields = writable(model)
    names = ", ".join(field["name"] for field in fields)
    placeholders = ", ".join(f"${index}" for index in range(1, len(fields) + 1))
    return (
        f"INSERT INTO {model['table']} ({names}) VALUES ({placeholders}) RETURNING {columns(model)}"
    )


def update_sql(model: dict[str, Any]) -> str:
    assignments = []
    for index, field in enumerate(writable(model)):
        flag, value = 2 * index + 2, 2 * index + 3
        assignments.append(
            f"{field['name']} = CASE WHEN ${flag} THEN ${value} ELSE {field['name']} END"
        )
    return (
        f"UPDATE {model['table']} SET {', '.join(assignments)} "
        f"WHERE {primary(model)['name']} = $1 RETURNING {columns(model)}"
    )


def replace_sql(model: dict[str, Any]) -> str:
    assignments = ", ".join(
        f"{field['name']} = ${index}" for index, field in enumerate(writable(model), start=2)
    )
    return (
        f"UPDATE {model['table']} SET {assignments} "
        f"WHERE {primary(model)['name']} = $1 RETURNING {columns(model)}"
    )


def delete_sql(model: dict[str, Any]) -> str:
    key = primary(model)["name"]
    return f"DELETE FROM {model['table']} WHERE {key} = $1 RETURNING {key}"


def conditions(model: dict[str, Any], partial: bool) -> str:
    """The canonical request body validation, as one JavaScript boolean expression."""
    fields = writable(model)
    allowed = json.dumps([field["name"] for field in fields])
    items = [
        'typeof body !== "object"',
        "body === null",
        "Array.isArray(body)",
        f"Object.keys(body).some((key) => !{allowed}.includes(key))",
    ]
    for field in fields:
        value = f"body.{field['name']}"
        if field["rust_type"] == "i32":
            invalid = [
                f'typeof {value} !== "number"',
                f"!Number.isInteger({value})",
                f"{value} < -2147483648",
                f"{value} > 2147483647",
            ]
        elif field["rust_type"] == "bool":
            invalid = [f'typeof {value} !== "boolean"']
        else:
            invalid = [f'typeof {value} !== "string"']
        if partial or field["nullable"]:
            guards = [f"{value} !== undefined"]
            if field["nullable"]:
                guards.append(f"{value} !== null")
            items.append(f"({' && '.join(guards)} && ({' || '.join(invalid)}))")
        else:
            items.extend(invalid)
    return " || ".join(items)


def values(model: dict[str, Any], partial: bool) -> str:
    parts = []
    for field in writable(model):
        value = f"body.{field['name']}"
        if partial:
            parts.append(f"{value} !== undefined")
            parts.append(f"{value} ?? null")
        else:
            parts.append(f"{value} ?? null" if field["nullable"] else value)
    return ", ".join(parts)


def _guarded(statement: str, conflict: bool) -> str:
    if not conflict:
        return statement
    return f"""try {{
{statement}
}} catch (error) {{
  if ((error as {{ code?: string }}).code === "23505") {{
    {CONFLICT}
  }}
  throw error;
}}"""


def handler_source(operation: str, model: dict[str, Any], param: str, tail_return: bool) -> str:
    """Render the canonical handler for ``operation`` against ``model``."""
    conflict = any(field["unique"] for field in writable(model))
    prefix = "return " if tail_return else ""
    lookup = f"""const id = Number({REQUEST}.params.{param});
if (!Number.isInteger(id)) {{
  {MISSING}
}}"""
    if operation == "lookup":
        body = f"""{lookup}
const {{ rows }} = await {POOL}.query("{select_sql(model)}", [id]);
if (rows.length === 0) {{
  {MISSING}
}}
{prefix}{RESPONSE}.json(rows[0]);"""
    elif operation == "delete":
        body = f"""{lookup}
const {{ rows }} = await {POOL}.query("{delete_sql(model)}", [id]);
if (rows.length === 0) {{
  {MISSING}
}}
{prefix}{RESPONSE}.status(204).end();"""
    elif operation == "create":
        query = f'await {POOL}.query("{insert_sql(model)}", [{values(model, False)}]);'
        body = f"""const body = {REQUEST}.body;
if ({conditions(model, False)}) {{
  {INVALID}
}}
""" + _guarded(
            f"""const {{ rows }} = {query}
{prefix}{RESPONSE}.status(201).json(rows[0]);""",
            conflict,
        )
    else:
        partial = operation == "update"
        sql = update_sql(model) if partial else replace_sql(model)
        body = f"""{lookup}
const body = {REQUEST}.body;
if ({conditions(model, partial)}) {{
  {INVALID}
}}
""" + _guarded(
            f"""const {{ rows }} = await {POOL}.query("{sql}", [id, {values(model, partial)}]);
if (rows.length === 0) {{
  {MISSING}
}}
{prefix}{RESPONSE}.json(rows[0]);""",
            conflict,
        )
    return f"export default async ({REQUEST}, {RESPONSE}) => {{\n{body}\n}};\n"


def idiom_shapes(
    models: list[dict[str, Any]], params: set[str], node: str | None = None
) -> dict[tuple[str, str, str], list[Any]]:
    """Parse every canonical handler once; keyed by (operation, model name, param)."""
    sources: dict[str, str] = {}
    for model in models:
        if not qualified(model):
            continue
        for operation in STATUSES:
            for param in sorted(params) or ["id"]:
                for tail_return in (True, False):
                    name = f"{operation}/{model['name']}/{param}/{int(tail_return)}.ts"
                    sources[name] = handler_source(operation, model, param, tail_return)
    if not sources:
        return {}
    parsed = parse_sources(sources, node=node)
    shapes: dict[tuple[str, str, str], list[Any]] = {}
    for name, item in parsed.items():
        if item.diagnostics:
            raise ValueError(f"internal idiom {name} failed to parse")
        statement = t.field_list(item.tree, "statements")[0]
        handler = t.field(statement, "expression")
        if handler is None:
            raise ValueError(f"internal idiom {name} has no handler")
        operation, model_name, param, _ = name[:-3].split("/")
        shapes.setdefault((operation, model_name, param), []).append(handler_shape(handler, {}))
    return shapes


def match_idiom(
    handler: t.Node,
    operation: str,
    param: str,
    models: list[dict[str, Any]],
    shapes: dict[tuple[str, str, str], list[Any]],
    names: tuple[str, str],
    pool: str | None,
) -> dict[str, Any]:
    """Return the capture record for a handler that equals one canonical idiom."""
    if pool is None:
        raise ValueError(
            f"{operation} handlers require a module-level pg Pool bound to DATABASE_URL"
        )
    if t.kind(handler) not in {"ArrowFunction", "FunctionExpression"}:
        raise ValueError("only inline handler functions are qualified")
    if "AsyncKeyword" not in t.modifier_kinds(handler):
        raise ValueError(f"{operation} handlers must be async")
    rename = {names[0]: REQUEST, names[1]: RESPONSE, pool: POOL}
    actual = handler_shape(handler, rename)
    for model in models:
        if actual in shapes.get((operation, model["name"], param), []):
            record: dict[str, Any] = {"model": model["name"], "table": model["table"]}
            if operation == "lookup":
                record["param"] = param
                return {"status": 200, "lookup": record}
            record["operation"] = operation
            if operation != "create":
                record["param"] = param
            if operation != "delete":
                record["conflict"] = any(field["unique"] for field in writable(model))
            return {"status": STATUSES[operation], "write": record}
    unqualified = [model["name"] for model in models if not qualified(model)]
    hint = f"; models with unsupported request fields: {unqualified}" if unqualified else ""
    raise ValueError(
        f"{operation} handlers must match the qualified idiom exactly "
        f"(see the extension README for the canonical {operation} handler){hint}"
    )

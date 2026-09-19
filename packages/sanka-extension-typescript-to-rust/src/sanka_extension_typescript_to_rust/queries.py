# SPDX-License-Identifier: Apache-2.0
"""Exact-shape recognition of bounded, primary-key-ordered flat table reads with ``pg``."""

from __future__ import annotations

import re
from typing import Any

from sanka_ts_capture import tree as t

_WHITESPACE = re.compile(r"\s+")


def normalize_sql(text: str) -> str:
    return _WHITESPACE.sub(" ", text.replace('"', "").strip().rstrip(";").strip()).lower()


def expected_sql(model: dict[str, Any]) -> str:
    columns = ", ".join(field["name"] for field in model["fields"])
    primary = next(field["name"] for field in model["fields"] if field["primary_key"])
    return f"select {columns} from {model['table']} order by {primary} limit $1"


def capture_read(
    statements: list[t.Node],
    response: str,
    pool: str | None,
    models: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return the read contract for ``const { rows } = await pool.query(sql, [n]); res.json(rows)``.

    Every deviation from that exact shape raises ``ValueError`` with the reason.
    """
    if len(statements) != 2 or t.kind(statements[0]) != "VariableStatement":
        raise ValueError("database reads must be exactly one awaited query and res.json(rows)")
    declarations = t.field(statements[0], "declarationList")
    items = t.field_list(declarations, "declarations") if declarations else []
    if declarations is None or declarations.get("decl") != "const" or len(items) != 1:
        raise ValueError("database reads must destructure const { rows } from one query")
    pattern = t.field(items[0], "name")
    elements = t.field_list(pattern, "elements") if pattern else []
    if (
        pattern is None
        or t.kind(pattern) != "ObjectBindingPattern"
        or len(elements) != 1
        or t.identifier_name(t.field(elements[0], "name")) != "rows"
        or t.field(elements[0], "propertyName") is not None
        or t.field(elements[0], "initializer") is not None
    ):
        raise ValueError("database reads must destructure const { rows } from one query")
    awaited = t.field(items[0], "initializer")
    if awaited is None or t.kind(awaited) != "AwaitExpression":
        raise ValueError("database reads must await pool.query(...)")
    parts = t.call_parts(t.field(awaited, "expression") or {})
    if pool is None:
        raise ValueError("database reads require a module-level pg Pool bound to DATABASE_URL")
    if parts is None or t.property_chain(parts[0]) != (pool, "query") or len(parts[1]) != 2:
        raise ValueError("database reads must call pool.query(sql, [limit]) exactly")
    sql = t.string_value(t.unparenthesize(parts[1][0]))
    values = t.unparenthesize(parts[1][1])
    limits = t.field_list(values, "elements") if t.kind(values) == "ArrayLiteralExpression" else []
    limit_text = t.text(t.unparenthesize(limits[0])) if len(limits) == 1 else None
    if (
        sql is None
        or limit_text is None
        or t.kind(t.unparenthesize(limits[0])) != "NumericLiteral"
        or not limit_text.isdigit()
        or not 1 <= int(limit_text) <= 1000
    ):
        raise ValueError(
            "database reads need a literal SQL string and one literal limit of 1 to 1000"
        )
    tail = statements[1]
    call = t.field(tail, "expression")
    if t.kind(tail) not in {"ExpressionStatement", "ReturnStatement"} or call is None:
        raise ValueError("database reads must respond with res.json(rows)")
    tail_parts = t.call_parts(call)
    if (
        tail_parts is None
        or t.property_chain(tail_parts[0]) != (response, "json")
        or len(tail_parts[1]) != 1
        or t.identifier_name(t.unparenthesize(tail_parts[1][0])) != "rows"
    ):
        raise ValueError("database reads must respond with res.json(rows)")
    normalized = normalize_sql(sql)
    for model in models:
        if normalized == expected_sql(model):
            primary = next(field["name"] for field in model["fields"] if field["primary_key"])
            return {
                "model": model["name"],
                "table": model["table"],
                "order_by": primary,
                "limit": int(limit_text),
            }
    raise ValueError(
        "database reads must select every column of one table in declaration order, "
        "ordered by its primary key, with LIMIT $1"
    )

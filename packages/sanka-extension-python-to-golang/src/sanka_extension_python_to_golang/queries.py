# SPDX-License-Identifier: Apache-2.0
"""Exact AST contracts for bounded, primary-key-ordered flat model reads."""

from __future__ import annotations

import ast
from typing import Any


def capture_read(
    node: ast.FunctionDef | ast.AsyncFunctionDef, framework: str, models: list[dict[str, Any]]
) -> dict[str, Any] | None:
    # Match the whole body, so filters, joins, side effects and altered projections
    # cannot be silently omitted. Limits must be present in the source itself.
    limits = {
        item.value
        for item in ast.walk(node)
        if isinstance(item, ast.Constant) and type(item.value) is int and 1 <= item.value <= 1000
    }
    actual = ast.dump(ast.Module(body=node.body, type_ignores=[]))
    for model in models:
        name = model["name"]
        fields = [field["name"] for field in model["fields"]]
        primary = next(field["name"] for field in model["fields"] if field["primary_key"])
        for limit in sorted(limits):
            if framework == "drf":
                projection = ", ".join(repr(field) for field in fields)
                body = (
                    f"return Response(list({name}.objects.order_by({primary!r})"
                    f".values({projection})[:{limit}]))"
                )
            else:
                projection = ", ".join(f"{name}.{field}" for field in fields)
                value = (
                    "[dict(row) for row in session.execute("
                    f"select({projection}).order_by({name}.{primary}).limit({limit})"
                    ").mappings()]"
                )
                if framework == "flask":
                    value = f"jsonify({value})"
                body = f"with Session(engine) as session:\n    return {value}"
            if actual == ast.dump(ast.parse(body)):
                if isinstance(node, ast.AsyncFunctionDef):
                    raise ValueError("synchronous database reads require a synchronous handler")
                return {"model": name, "order_by": primary, "limit": limit}
    return None

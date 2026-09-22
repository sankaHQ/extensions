# SPDX-License-Identifier: Apache-2.0
"""Exact AST contracts for bounded, primary-key-ordered flat model reads."""

from __future__ import annotations

import ast
import copy
from typing import Any

from .values import output_value, uuid_lookup_prefix


def _pagination(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, dict[str, str]] | None:
    """Match explicit bounded ASCII query validation before normalizing the query."""
    if len(node.args.args) < 2 or len(node.args.defaults) < 2:
        return None
    if [ast.unparse(arg) for arg in node.args.args[-2:]] != ["limit: str", "offset: str"]:
        return None
    defaults = node.args.defaults[-2:]
    if not all(isinstance(item, ast.Constant) and type(item.value) is str for item in defaults):
        return None
    limit, offset = (ast.literal_eval(item) for item in defaults)
    if not (
        limit.isascii()
        and limit.isdecimal()
        and len(limit) <= 4
        and 1 <= int(limit) <= 1000
        and offset.isascii()
        and offset.isdecimal()
        and len(offset) <= 10
        and 0 <= int(offset) <= 2147483647
    ):
        raise ValueError("pagination defaults must satisfy the captured bounds")
    if len({arg.arg for arg in node.args.args}) != len(node.args.args):
        raise ValueError("pagination parameter names must be unique")
    if len(node.body) != 1 or not isinstance(node.body[0], ast.With):
        return None
    scope = node.body[0]
    guard = ast.parse("""if not (limit.isascii() and limit.isdecimal() and len(limit) <= 4
            and 1 <= int(limit) <= 1000 and offset.isascii() and offset.isdecimal()
            and len(offset) <= 10 and 0 <= int(offset) <= 2147483647):
    raise HTTPException(status_code=400, detail="invalid pagination")
""").body[0]
    if len(scope.body) != 2 or ast.dump(scope.body[0]) != ast.dump(guard):
        return None
    lowered = copy.deepcopy(node)
    lowered.args.args = lowered.args.args[:-2]
    lowered.args.defaults = lowered.args.defaults[:-2]
    lowered_scope = lowered.body[0]
    assert isinstance(lowered_scope, ast.With)
    lowered_scope.body.pop(0)

    class Page(ast.NodeTransformer):
        count = 0

        def visit_Call(self, item: ast.Call) -> ast.AST:
            if (
                isinstance(item.func, ast.Attribute)
                and item.func.attr == "offset"
                and len(item.args) == 1
                and ast.unparse(item.args[0]) == "int(offset)"
            ) and not item.keywords:
                limited = item.func.value
                if (
                    isinstance(limited, ast.Call)
                    and isinstance(limited.func, ast.Attribute)
                    and limited.func.attr == "limit"
                    and len(limited.args) == 1
                    and ast.unparse(limited.args[0]) == "int(limit)"
                    and not limited.keywords
                ):
                    self.count += 1
                    limited.args = [ast.Constant(value=int(limit))]
                    return limited
            return self.generic_visit(item)

    page = Page()
    page.visit(lowered)
    if page.count != 1:
        return None
    return lowered, {"limit": limit, "offset": offset}


def capture_read(
    node: ast.FunctionDef | ast.AsyncFunctionDef, framework: str, models: list[dict[str, Any]]
) -> dict[str, Any] | None:
    page = _pagination(node) if framework == "fastapi" else None
    if page:
        lowered, pagination = page
        result = capture_read(lowered, framework, models)
        if result is not None and "lookup" not in result:
            result["pagination"] = pagination
            return result
        return None
    referenced = {item.id for item in ast.walk(node) if isinstance(item, ast.Name)}
    models = [model for model in models if model["name"] in referenced]
    if not models:
        return None
    # Match the whole body, so filters, joins, side effects and altered projections
    # cannot be silently omitted. Limits must be present in the source itself.
    actual = ast.dump(ast.Module(body=node.body, type_ignores=[]))
    for model in models:
        fields = model["fields"]
        primary = next(field for field in fields if field["primary_key"])
        response = (
            "{"
            + ", ".join(
                repr(field["name"]) + ": " + output_value(field, "item." + field["name"])
                for field in fields
            )
            + "}"
        )
        if framework == "drf":
            signature = f"request, {primary['name']}"
            lookup = (
                f"item = {model['name']}.objects.filter("
                f"{primary['name']}={primary['name']}).first()\n"
            )
            source = (
                lookup
                + f"""if item is None:
    return Response({{"error": "not found"}}, status=404)
return Response({response})
"""
            )
        else:
            signature = (
                primary["name"]
                if framework == "flask"
                else f"{primary['name']}: {'str' if primary['go_type'] == 'UUIDValue' else 'int'}"
            )
            missing = (
                'return jsonify({"error": "not found"}), 404'
                if framework == "flask"
                else 'raise HTTPException(status_code=404, detail="not found")'
            )
            response_value = f"jsonify({response})" if framework == "flask" else response
            source = f"""with Session(engine) as session:
    item = session.get({model["name"]}, {primary["name"]})
    if item is None:
        {missing}
    return {response_value}
"""
        source = uuid_lookup_prefix(primary, framework) + source
        expected = ast.parse(
            f"def endpoint({signature}):\n"
            + "\n".join("    " + line for line in source.splitlines())
        ).body[0]
        assert isinstance(expected, ast.FunctionDef)
        if (
            not isinstance(node, ast.AsyncFunctionDef)
            and ast.dump(node.args) == ast.dump(expected.args)
            and actual == ast.dump(ast.Module(body=expected.body, type_ignores=[]))
        ):
            return {"model": model["name"], "lookup": primary["name"]}
    if len(node.body) != 1:
        return None
    limits = {
        item.value
        for item in ast.walk(node)
        if isinstance(item, ast.Constant) and type(item.value) is int and 1 <= item.value <= 1000
    }
    parameters: list[tuple[str, str, str]] = []
    if framework == "fastapi" and len(node.args.args) == 1 and len(node.args.defaults) == 1:
        arg, default_node = node.args.args[0], node.args.defaults[0]
        reserved = {
            "engine",
            "session",
            "Session",
            "select",
            "dict",
            "row",
            "str",
            "int",
            "len",
            "HTTPException",
        }
        reserved.update(model["name"] for model in models)
        if (ast.unparse(arg.annotation) == "str" if arg.annotation else False) and (
            isinstance(default_node, ast.Constant)
            and type(default_node.value) is str
            and arg.arg not in reserved
        ):
            parameters.append((arg.arg, default_node.value, arg.arg))
    elif framework in {"drf", "flask"}:
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and len(call.args) == 2
                and all(
                    isinstance(value, ast.Constant) and type(value.value) is str
                    for value in call.args
                )
            ):
                key, default = (ast.literal_eval(value) for value in call.args)
                accessor = "request.query_params" if framework == "drf" else "request.args"
                expression = f"{accessor}.get({key!r}, {default!r})"
                if ast.dump(call) == ast.dump(ast.parse(expression, mode="eval").body):
                    parameters.append((key, default, expression))
    for key, default, _ in parameters:
        try:
            key.encode("utf-8")
            default.encode("utf-8")
        except UnicodeError as error:
            raise ValueError("query names and defaults require valid Unicode") from error
    for model in models:
        name = model["name"]
        fields = [field["name"] for field in model["fields"]]
        rich = any(field["go_type"].endswith("Value") for field in model["fields"])
        projection_dict = (
            "{"
            + ", ".join(
                repr(field["name"]) + ": " + output_value(field, f"row[{field['name']!r}]")
                for field in model["fields"]
            )
            + "}"
        )
        primary = next(field["name"] for field in model["fields"] if field["primary_key"])
        filters: list[dict[str, Any] | None] = [None]
        filters.extend(
            {
                "field": field["name"],
                "parameter": field["name"],
                "expression": field["name"],
                "path": True,
            }
            for field in model["fields"]
            if "references" in field
            and field["go_type"] in {"int32", "int64"}
            and field["name"]
            not in {"request", "engine", "session", "select", "dict", "row", "int"}
        )
        filters.extend(
            {"field": field["name"], "parameter": key, "default": default, "expression": expression}
            for field in model["fields"]
            if field["go_type"] == "string"
            for key, default, expression in parameters
        )
        for filtered in filters:
            signature = "request" if framework == "drf" else ""
            if filtered and filtered.get("path"):
                signature = {
                    "drf": f"request, {filtered['parameter']}",
                    "flask": filtered["parameter"],
                    "fastapi": f"{filtered['parameter']}: int",
                }[framework]
            elif filtered and framework == "fastapi":
                signature = f"{filtered['parameter']}: str = {filtered['default']!r}"
            expected_function = ast.parse(f"def endpoint({signature}): pass").body[0]
            assert isinstance(expected_function, ast.FunctionDef)
            expected_args = expected_function.args
            if ast.dump(node.args) != ast.dump(expected_args):
                continue
            for limit in sorted(limits):
                if framework == "drf":
                    projection = ", ".join(repr(field) for field in fields)
                    where = (
                        f".filter({filtered['field']}={filtered['expression']})" if filtered else ""
                    )
                    rows = (
                        f"{name}.objects{where}.order_by({primary!r})"
                        f".values({projection})[:{limit}]"
                    )
                    value = f"[{projection_dict} for row in {rows}]" if rich else f"list({rows})"
                    body = f"return Response({value})"
                else:
                    projection = ", ".join(f"{name}.{field}" for field in fields)
                    where = (
                        f".where({name}.{filtered['field']} == {filtered['expression']})"
                        if filtered
                        else ""
                    )
                    value = (
                        f"[{projection_dict if rich else 'dict(row)'} for row in session.execute("
                        f"select({projection}){where}.order_by({name}.{primary}).limit({limit})"
                        ").mappings()]"
                    )
                    if framework == "flask":
                        value = f"jsonify({value})"
                    body = f"with Session(engine) as session:\n    return {value}"
                if actual == ast.dump(ast.parse(body)):
                    if isinstance(node, ast.AsyncFunctionDef):
                        raise ValueError("synchronous database reads require a synchronous handler")
                    result = {"model": name, "order_by": primary, "limit": limit}
                    if filtered and filtered.get("path"):
                        result.update(lookup=filtered["field"], many=True)
                    elif filtered:
                        result["filter"] = {
                            key: value for key, value in filtered.items() if key != "expression"
                        }
                    return result
    return None

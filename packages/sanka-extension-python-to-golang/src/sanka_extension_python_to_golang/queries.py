# SPDX-License-Identifier: Apache-2.0
"""Exact AST contracts for bounded, primary-key-ordered flat model reads."""

from __future__ import annotations

import ast
from typing import Any


def capture_read(
    node: ast.FunctionDef | ast.AsyncFunctionDef, framework: str, models: list[dict[str, Any]]
) -> dict[str, Any] | None:
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
            + ", ".join(repr(field["name"]) + ": item." + field["name"] for field in fields)
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
            signature = primary["name"] if framework == "flask" else f"{primary['name']}: int"
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
        reserved = {"engine", "session", "Session", "select", "dict", "row", "str"}
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
        primary = next(field["name"] for field in model["fields"] if field["primary_key"])
        filters: list[dict[str, str] | None] = [None]
        filters.extend(
            {"field": field["name"], "parameter": key, "default": default, "expression": expression}
            for field in model["fields"]
            if field["go_type"] == "string"
            for key, default, expression in parameters
        )
        for filtered in filters:
            signature = "request" if framework == "drf" else ""
            if filtered and framework == "fastapi":
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
                    body = (
                        f"return Response(list({name}.objects{where}.order_by({primary!r})"
                        f".values({projection})[:{limit}]))"
                    )
                else:
                    projection = ", ".join(f"{name}.{field}" for field in fields)
                    where = (
                        f".where({name}.{filtered['field']} == {filtered['expression']})"
                        if filtered
                        else ""
                    )
                    value = (
                        "[dict(row) for row in session.execute("
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
                    if filtered:
                        result["filter"] = {
                            key: value for key, value in filtered.items() if key != "expression"
                        }
                    return result
    return None

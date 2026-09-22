# SPDX-License-Identifier: Apache-2.0
"""Exact AST contracts for bounded, primary-key-ordered flat model reads."""

from __future__ import annotations

import ast
import copy
from typing import Any

from .values import output_value, uuid_lookup_prefix


def _pagination(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    framework: str,
) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, dict[str, str]] | None:
    """Match explicit bounded ASCII query validation before normalizing the query."""
    lowered = copy.deepcopy(node)
    if framework == "fastapi":
        if len(node.args.args) < 2 or len(node.args.defaults) < 2:
            return None
        if [ast.unparse(arg) for arg in node.args.args[-2:]] != ["limit: str", "offset: str"]:
            return None
        defaults = node.args.defaults[-2:]
        if not all(isinstance(item, ast.Constant) and type(item.value) is str for item in defaults):
            return None
        limit, offset = (ast.literal_eval(item) for item in defaults)
        lowered.args.args = lowered.args.args[:-2]
        lowered.args.defaults = lowered.args.defaults[:-2]
    else:
        body = (
            lowered.body
            if framework == "drf"
            else (
                lowered.body[0].body
                if len(lowered.body) == 1 and isinstance(lowered.body[0], ast.With)
                else []
            )
        )
        if len(body) != 4:
            return None
        query_defaults = []
        accessor = "request.query_params" if framework == "drf" else "request.args"
        for statement, name in zip(body[:2], ("limit", "offset"), strict=True):
            if not isinstance(statement, ast.Assign) or not isinstance(statement.value, ast.Call):
                return None
            call = statement.value
            if (
                len(call.args) != 2
                or not isinstance(call.args[1], ast.Constant)
                or type(call.args[1].value) is not str
            ):
                return None
            default = call.args[1].value
            expected = ast.parse(f"{name} = {accessor}.get({name!r}, {default!r})").body[0]
            if ast.dump(statement) != ast.dump(expected):
                return None
            query_defaults.append(default)
        limit, offset = query_defaults
        del body[:2]
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
    if framework == "drf":
        body = lowered.body
    elif len(lowered.body) == 1 and isinstance(lowered.body[0], ast.With):
        body = lowered.body[0].body
    else:
        return None
    error = {
        "drf": 'return Response({"error": "invalid pagination"}, status=400)',
        "flask": 'return jsonify({"error": "invalid pagination"}), 400',
        "fastapi": 'raise HTTPException(status_code=400, detail="invalid pagination")',
    }[framework]
    guard = ast.parse(
        """if not (limit.isascii() and limit.isdecimal() and len(limit) <= 4
            and 1 <= int(limit) <= 1000 and offset.isascii() and offset.isdecimal()
            and len(offset) <= 10 and 0 <= int(offset) <= 2147483647):
    """
        + error
    ).body[0]
    if len(body) != 2 or ast.dump(body[0]) != ast.dump(guard):
        return None
    body.pop(0)

    class Page(ast.NodeTransformer):
        count = 0

        def visit_Subscript(self, item: ast.Subscript) -> ast.AST:
            expected = ast.parse("rows[int(offset):int(offset) + int(limit)]", mode="eval").body
            assert isinstance(expected, ast.Subscript)
            if framework == "drf" and ast.dump(item.slice) == ast.dump(expected.slice):
                self.count += 1
                item.slice = ast.Slice(upper=ast.Constant(value=int(limit)))
                return item
            return self.generic_visit(item)

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


def _combined_filters(
    node: ast.FunctionDef | ast.AsyncFunctionDef, framework: str, models: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Qualify each AND operand through the existing single-predicate matcher."""
    method = "filter" if framework == "drf" else "where"
    calls = [
        n
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == method
    ]
    if len(calls) != 1:
        return None
    call = calls[0]
    operands = call.keywords if framework == "drf" else call.args
    if len(operands) < 2 or len(operands) > 16:
        return None
    if call.args if framework == "drf" else call.keywords:
        return None
    if framework == "drf" and len({item.arg for item in call.keywords}) != len(call.keywords):
        return None
    results = []
    used_parameters = set()
    for index in range(len(operands)):
        candidate = copy.deepcopy(node)
        selected = next(
            n
            for n in ast.walk(candidate)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == method
        )
        if framework == "drf":
            selected.keywords = [selected.keywords[index]]
        else:
            selected.args = [selected.args[index]]
        if framework == "fastapi":
            names = {n.id for n in ast.walk(selected) if isinstance(n, ast.Name)}
            if len(candidate.args.args) != len(candidate.args.defaults):
                return None
            pairs = [
                (a, d)
                for a, d in zip(candidate.args.args, candidate.args.defaults, strict=True)
                if a.arg in names
            ]
            candidate.args.args = [a for a, _ in pairs]
            candidate.args.defaults = [d for _, d in pairs]
        result = capture_read(candidate, framework, models)
        if result is None or "filter" not in result:
            return None
        used_parameters.add(result["filter"]["parameter"])
        results.append(result)
    if framework == "fastapi" and used_parameters != {a.arg for a in node.args.args}:
        return None
    base = {k: v for k, v in results[0].items() if k != "filter"}
    if any({k: v for k, v in r.items() if k != "filter"} != base for r in results):
        return None
    return base | {"filters": [r["filter"] for r in results]}


def capture_read(
    node: ast.FunctionDef | ast.AsyncFunctionDef, framework: str, models: list[dict[str, Any]]
) -> dict[str, Any] | None:
    page = _pagination(node, framework)
    if page:
        lowered, pagination = page
        result = capture_read(lowered, framework, models)
        if result is not None and "lookup" not in result:
            result["pagination"] = pagination
            return result
        return None
    combined = _combined_filters(node, framework, models)
    if combined:
        return combined
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
    if len(node.body) not in {1, 3}:
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
                "go_type": field["go_type"],
            }
            for field in model["fields"]
            if "references" in field
            and field["go_type"] in {"int32", "int64", "UUIDValue"}
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
                kind = "str" if filtered["go_type"] == "UUIDValue" else "int"
                signature = {
                    "drf": f"request, {filtered['parameter']}",
                    "flask": filtered["parameter"],
                    "fastapi": f"{filtered['parameter']}: {kind}",
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
                if filtered and filtered.get("path"):
                    body = (
                        uuid_lookup_prefix(
                            {"name": filtered["field"], "go_type": filtered["go_type"]}, framework
                        )
                        + body
                    )
                if actual == ast.dump(ast.parse(body)):
                    if isinstance(node, ast.AsyncFunctionDef):
                        raise ValueError("synchronous database reads require a synchronous handler")
                    result = {"model": name, "order_by": primary, "limit": limit}
                    if filtered and filtered.get("path"):
                        result.update(lookup=filtered["field"], many=True)
                        if filtered["go_type"] == "UUIDValue":
                            result["lookup_type"] = "uuid"
                    elif filtered:
                        result["filter"] = {
                            key: value for key, value in filtered.items() if key != "expression"
                        }
                    return result
    return None

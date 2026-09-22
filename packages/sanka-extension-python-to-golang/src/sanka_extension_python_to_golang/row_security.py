# SPDX-License-Identifier: Apache-2.0
"""Normalize explicit identity predicates only around qualified ORM operations."""

from __future__ import annotations

import ast
from typing import Any


def normalize_row_security(
    tree: ast.Module, framework: str, security: dict[str, Any]
) -> dict[str, Any]:
    if not security.get("native"):
        return {}
    scopes: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        receiver = security["receivers"].get(node.name)
        if receiver is None and framework == "drf" and "__sanka_" in node.name:
            receiver = security["receivers"].get(node.name.split("__sanka_")[0])
        if receiver is None:
            continue
        root = receiver.split(".")[0]
        if any(
            isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id == root
            for n in ast.walk(node)
        ):
            raise ValueError("verified principal must not be rebound in a handler")

        def claim(value: ast.expr, receiver: str = receiver) -> str | None:
            if framework == "drf" and ast.unparse(value) == "request.user.pk":
                return "sub"
            if (
                isinstance(value, ast.Subscript)
                and ast.unparse(value.value) == receiver
                and isinstance(value.slice, ast.Constant)
                and value.slice.value in ("sub", "tenant")
            ):
                return str(value.slice.value)
            return None

        row: dict[str, str] = {}
        body: dict[str, str] = {}
        models: set[str] = set()

        def record(target: dict[str, str], field: str, value: ast.expr) -> bool:
            identity = claim(value)
            if identity is None:
                return False
            if field in target:
                raise ValueError("duplicate identity predicate")
            target[field] = identity
            return True

        # Only remove a body guard immediately before the qualified persistence scope.
        data = "request.data" if framework == "drf" else "data"
        denied = {
            "drf": 'return Response({"error": "permission denied"}, status=403)',
            "flask": 'return jsonify({"error": "permission denied"}), 403',
            "fastapi": 'raise HTTPException(status_code=403, detail="permission denied")',
        }[framework]
        transactional = any(
            isinstance(n, ast.With)
            and any(
                ast.unparse(i.context_expr) in {"transaction.atomic()", "session.begin()"}
                for i in n.items
            )
            for n in ast.walk(node)
        )
        if transactional:
            steps: dict[str, Any] = {}
            for entry in ast.walk(node):
                if (
                    not isinstance(entry, ast.If)
                    or not isinstance(entry.test, ast.BoolOp)
                    or not isinstance(entry.test.op, ast.Or)
                ):
                    continue
                terms = entry.test.values
                first = terms[0]
                if not isinstance(first, ast.Compare) or not isinstance(first.left, ast.Name):
                    continue
                variable = first.left.id
                if ast.unparse(first) != f"{variable} is None":
                    continue
                found: dict[str, str] = {}
                for term in terms[1:]:
                    if not (
                        isinstance(term, ast.Compare)
                        and len(term.ops) == 1
                        and isinstance(term.ops[0], ast.NotEq)
                        and isinstance(term.left, ast.Attribute)
                        and ast.unparse(term.left.value) == variable
                        and record(found, term.left.attr, term.comparators[0])
                    ):
                        break
                else:
                    if variable in steps:
                        raise ValueError("duplicate transaction row guard")
                    steps[variable] = {"row": found, "body": {}, "models": []}
                    entry.test = first
            # All request guards must be consecutive immediately before the transaction.
            scope_index = next((i for i, n in enumerate(node.body) if isinstance(n, ast.With)), -1)
            for index in range(scope_index - 1, -1, -1):
                entry = node.body[index]
                if (
                    not isinstance(entry, ast.If)
                    or not isinstance(entry.test, ast.BoolOp)
                    or not isinstance(entry.test.op, ast.And)
                    or len(entry.test.values) != 2
                ):
                    break
                comparison = entry.test.values[1]
                if not (
                    isinstance(comparison, ast.Compare)
                    and len(comparison.ops) == 1
                    and isinstance(comparison.ops[0], ast.NotEq)
                    and isinstance(comparison.left, ast.Subscript)
                    and isinstance(comparison.left.slice, ast.Constant)
                    and type(comparison.left.slice.value) is str
                ):
                    break
                access = comparison.left.value
                if not (
                    isinstance(access, ast.Subscript)
                    and ast.unparse(access.value) == data
                    and isinstance(access.slice, ast.Constant)
                    and type(access.slice.value) is str
                ):
                    break
                variable, field = access.slice.value, comparison.left.slice.value
                expected = ast.parse(
                    f"if {field!r} in {ast.unparse(access)} and "
                    f"{ast.unparse(comparison)}:\n    {denied}"
                ).body[0]
                if ast.dump(entry) != ast.dump(expected):
                    break
                policy = steps.setdefault(variable, {"row": {}, "body": {}, "models": []})
                if not record(policy["body"], field, comparison.comparators[0]):
                    break
                node.body.pop(index)
            if steps:
                scopes[node.name] = {"transaction": steps}
            continue
        for index in range(len(node.body) - 2, -1, -1):
            item = node.body[index]
            following = node.body[index + 1]
            if not (
                isinstance(following, ast.With)
                or (
                    isinstance(following, ast.Assign)
                    and [ast.unparse(t) for t in following.targets] == ["item"]
                )
            ):
                continue
            if not isinstance(item, ast.If) or item.orelse:
                continue
            test = item.test
            if not (
                isinstance(test, ast.BoolOp)
                and isinstance(test.op, ast.And)
                and len(test.values) == 2
            ):
                continue
            comparison = test.values[1]
            if not (
                isinstance(comparison, ast.Compare)
                and len(comparison.ops) == 1
                and isinstance(comparison.ops[0], ast.NotEq)
                and isinstance(comparison.left, ast.Subscript)
                and isinstance(comparison.left.slice, ast.Constant)
                and type(comparison.left.slice.value) is str
            ):
                continue
            field = comparison.left.slice.value
            expected = ast.parse(
                f"if {field!r} in {data} and {data}[{field!r}] != "
                f"{ast.unparse(comparison.comparators[0])}:\n    {denied}"
            ).body[0]
            if ast.dump(item) == ast.dump(expected) and record(
                body, field, comparison.comparators[0]
            ):
                node.body.pop(index)

        # The remaining complete body must still match capture's existing ORM recipe.
        for entry in ast.walk(node):
            if (
                isinstance(entry, ast.If)
                and isinstance(entry.test, ast.BoolOp)
                and isinstance(entry.test.op, ast.Or)
            ):
                terms = entry.test.values
                if ast.unparse(terms[0]) != "item is None":
                    continue
                found = {}
                for term in terms[1:]:
                    if not (
                        isinstance(term, ast.Compare)
                        and len(term.ops) == 1
                        and isinstance(term.ops[0], ast.NotEq)
                        and isinstance(term.left, ast.Attribute)
                        and ast.unparse(term.left.value) == "item"
                        and record(found, term.left.attr, term.comparators[0])
                    ):
                        break
                else:
                    if row:
                        raise ValueError("multiple row policy guards require capture")
                    row = found
                    entry.test = terms[0]

        class Predicate(ast.NodeTransformer):
            def visit_Call(
                self, call: ast.Call, row: dict[str, str] = row, models: set[str] = models
            ) -> ast.AST:
                if isinstance(call.func, ast.Attribute):
                    base = call.func.value
                    if (
                        framework == "drf"
                        and call.func.attr == "filter"
                        and not call.args
                        and isinstance(base, ast.Attribute)
                        and base.attr == "objects"
                        and isinstance(base.value, ast.Name)
                    ):
                        found: dict[str, str] = {}
                        for kw in call.keywords:
                            if kw.arg is None or not record(found, kw.arg, kw.value):
                                break
                        else:
                            if found:
                                if row:
                                    raise ValueError("multiple row predicates require capture")
                                row.update(found)
                                models.add(base.value.id)
                                return base
                    if (
                        framework != "drf"
                        and call.func.attr == "where"
                        and not call.keywords
                        and isinstance(base, ast.Call)
                        and ast.unparse(base.func) == "select"
                    ):
                        found = {}
                        for term in call.args:
                            if not (
                                isinstance(term, ast.Compare)
                                and len(term.ops) == 1
                                and isinstance(term.ops[0], ast.Eq)
                                and isinstance(term.left, ast.Attribute)
                                and isinstance(term.left.value, ast.Name)
                                and record(found, term.left.attr, term.comparators[0])
                            ):
                                break
                            models.add(term.left.value.id)
                        else:
                            if found:
                                if row:
                                    raise ValueError("multiple row predicates require capture")
                                row.update(found)
                                return base
                return self.generic_visit(call)

        Predicate().visit(node)
        if row or body:
            scopes[node.name] = {"row": row, "body": body, "models": sorted(models)}
    if (
        scopes
        and framework == "flask"
        and not any(isinstance(n, ast.Name) and n.id == "g" for n in ast.walk(tree))
    ):
        for statement in tree.body:
            if isinstance(statement, ast.ImportFrom) and statement.module == "flask":
                statement.names = [a for a in statement.names if a.name != "g"]
        tree.body = [n for n in tree.body if not isinstance(n, ast.ImportFrom) or n.names]
    return scopes


def attach_scope(
    payload: dict[str, Any], scope: dict[str, Any], models: list[dict[str, Any]]
) -> None:
    operation = payload.get("read", payload.get("write"))
    if operation is None:
        raise ValueError("identity predicates require qualified database operations")
    if operation.get("transaction"):
        policies = scope.get("transaction", {})
        steps = operation["transaction"]
        if set(policies) != {step["input"] for step in steps}:
            raise ValueError("every transaction step requires an explicit identity policy")
        for step in steps:
            kind = step.get("operation", "create")
            child = {"operation": kind, "model": step["model"]}
            attach_scope(
                {"read" if kind == "lookup" else "write": child}, policies[step["input"]], models
            )
            step["scope"] = child["scope"]
            # An input foreign key cannot establish that the related row is accessible.
            model = next(m for m in models if m["name"] == step["model"])
            if kind in {"create", "replace", "patch"} and any(
                f.get("references") and f["name"] not in step["references"] for f in model["fields"]
            ):
                raise ValueError(
                    "scoped transaction foreign keys require earlier scoped record references"
                )
        operation["scope"] = dict(
            sorted({k: v for step in steps for k, v in step["scope"].items()}.items())
        )
        return
    row, body = scope["row"], scope["body"]
    creating = operation.get("operation") == "create"
    writing = "write" in payload and operation["operation"] != "delete"
    if (
        (creating and row)
        or (not creating and not row)
        or (writing and not creating and body != row)
        or (not writing and body)
    ):
        raise ValueError("row and body identity predicates must agree")
    effective = body if creating else row
    if not effective or (scope["models"] and scope["models"] != [operation["model"]]):
        raise ValueError("identity predicates must refer to the captured model")
    model = next(m for m in models if m["name"] == operation["model"])
    fields = {f["name"]: f for f in model["fields"]}
    if any(
        name not in fields
        or fields[name]["go_type"] != "string"
        or fields[name]["nullable"]
        or fields[name]["primary_key"]
        for name in effective
    ):
        raise ValueError("identity columns must be nonnullable, non-primary string fields")
    operation["scope"] = dict(sorted(effective.items()))


def scoped_replay_roles(
    case: dict[str, Any], roles: tuple[tuple[str, str, int], ...], routes: list[dict[str, Any]]
) -> tuple[tuple[tuple[str, str, int], ...], dict[str, Any]]:
    import re
    from urllib.parse import urlsplit

    from .jwt_security import REPLAY_JWT_ENV, replay_token

    path = urlsplit(case["path"]).path
    for route in routes:
        pattern = "/".join(
            "[^/]+" if part.startswith(":") else re.escape(part)
            for part in route["path"].split("/")
        )
        if route["method"] != case["method"] or re.fullmatch(pattern, path) is None:
            continue
        operation = route.get("read", route.get("write", {}))
        scope = operation.get("scope")
        if not scope:
            return roles, {}
        # Remove the generic alternate identity: scoped detail reads return 404.
        roles = tuple(role for role in roles if role[0] != "other-identity")
        if case["expected_status"] not in (200, 201, 204, 404, 409):
            return roles, {}
        probes = []
        bodies: dict[str, Any] = {}
        steps = operation.get("transaction", [])
        claims_used = (
            {value for step in steps for value in step["scope"].values()}
            if steps
            else set(scope.values())
        )
        for claim in sorted(claims_used):
            body = case.get("body", {})
            guarded_body = case["method"] in ("POST", "PUT", "PATCH") and any(
                field in body for field, value in scope.items() if value == claim
            )
            status = 403 if guarded_body else (404 if operation.get("lookup") else 200)
            claims = {
                "iss": REPLAY_JWT_ENV["AUTH_JWT_ISSUER"],
                "aud": REPLAY_JWT_ENV["AUTH_JWT_AUDIENCE"],
                "sub": "fixture-user",
                "tenant": "fixture-tenant",
                "role": "writer",
                "exp": 4102444800,
            }
            claims[claim] = "other-user" if claim == "sub" else "other-tenant"
            if steps:
                guarded_body = any(
                    step.get("operation", "create") in {"create", "replace", "patch"}
                    and any(
                        field in body.get(step["input"], {})
                        for field, value in step["scope"].items()
                        if value == claim
                    )
                    for step in steps
                )
                existing_row = any(
                    step.get("operation", "create") != "create"
                    and not step.get("lookup_reference")
                    and claim in step["scope"].values()
                    for step in steps
                )
                if not guarded_body and not existing_row:
                    continue
                status = 403 if guarded_body else 404
                probes.append(("transaction-cross-" + claim, replay_token(claims), status))
                if guarded_body and existing_row:
                    role = "transaction-cross-row-" + claim
                    bodies[role] = {key: dict(value) for key, value in body.items()}
                    for step in steps:
                        if step.get("operation", "create") in {"create", "replace", "patch"}:
                            bodies[role][step["input"]].update(
                                {field: claims[value] for field, value in step["scope"].items()}
                            )
                    probes.append((role, replay_token(claims), 404))
                continue
            probes.append(("cross-" + claim, replay_token(claims), status))
            if case["method"] in ("PUT", "PATCH") and operation.get("lookup"):
                role = "cross-row-" + claim
                bodies[role] = dict(body) | {field: claims[value] for field, value in scope.items()}
                probes.append((role, replay_token(claims), 404))
        # Probe before deletion/replacement; otherwise a lost ownership predicate could hide.
        return tuple(probes) + roles, bodies
    return roles, {}

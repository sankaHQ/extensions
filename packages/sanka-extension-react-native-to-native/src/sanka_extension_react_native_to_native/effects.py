# SPDX-License-Identifier: Apache-2.0
"""Slice-2 effects: exact-shape ``fetch`` loaders and submitters, AsyncStorage reads.

An effect is an ``async`` function with no parameters whose body is a run of flag
assignments, one ``try`` around ``await fetch(...)``, an optional ``response.ok``
check, an optional ``await response.json()`` and the success actions, an optional
``catch`` limited to flag assignments (none for submissions) and an optional
``finally`` limited to flag assignments. ``useEffect(() => { ... }, [])`` may call
effect functions, chain ``fetch(url).then(r => r.json()).then(setX)`` or read
``AsyncStorage.getItem(key).then(v => setX(v ?? literal))``. Everything else is a
``SANKA_RN_NETWORK`` or ``SANKA_RN_STORAGE`` reason code.
"""

from __future__ import annotations

from typing import Any

from sanka_ts_capture import tree as t

from .expressions import Expr, Scope, Unsupported, is_literal, lower, lower_handler

NETWORK = "SANKA_RN_NETWORK"
STORAGE = "SANKA_RN_STORAGE"
METHODS = ("GET", "POST")
ANONYMOUS_LOADER = "load"


def _statements(block: t.Node | None) -> list[t.Node]:
    return t.field_list(block, "statements") if block is not None else []


def _expression(statement: t.Node) -> t.Node | None:
    if t.kind(statement) != "ExpressionStatement":
        return None
    return t.field(statement, "expression")


def _flag_actions(statements: list[t.Node], scope: Scope, where: str) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for statement in statements:
        expression = _expression(statement)
        if expression is None:
            raise Unsupported(NETWORK, f"{where}: only state flag assignments are qualified")
        try:
            action = lower_handler(_arrow(expression), scope)[0]
        except Unsupported as error:
            raise Unsupported(
                NETWORK, f"{where}: only literal state assignments are qualified ({error.message})"
            ) from error
        if "set" not in action or not is_literal(action["value"]):
            raise Unsupported(NETWORK, f"{where}: only literal state assignments are qualified")
        actions.append(action)
    return actions


def _arrow(expression: t.Node) -> t.Node:
    """Wrap an expression so ``lower_handler`` sees a one-action arrow body."""
    return {"k": "ArrowFunction", "s": 0, "e": 0, "f": {"parameters": [], "body": expression}}


def is_async_function(node: t.Node) -> bool:
    return t.kind(node) in {
        "ArrowFunction",
        "FunctionExpression",
        "FunctionDeclaration",
    } and t.modifier_kinds(node) == {"AsyncKeyword"}


def capture_effect_function(node: t.Node, name: str, scope: Scope, rel: str) -> dict[str, Any]:
    """Lower an async effect function to the fetch effect IR."""
    where = f"{rel}: effect {name}"
    if t.field_list(node, "parameters"):
        raise Unsupported(NETWORK, f"{where} must take no parameters")
    body = t.field(node, "body")
    if body is None or t.kind(body) != "Block":
        raise Unsupported(NETWORK, f"{where} needs a block body")
    statements = _statements(body)
    index = next(
        (position for position, item in enumerate(statements) if t.kind(item) == "TryStatement"),
        None,
    )
    if index is None or index != len(statements) - 1:
        raise Unsupported(NETWORK, f"{where} must end with one try statement around fetch")
    before = _flag_actions(statements[:index], scope, where)
    try_statement = statements[index]
    effect = _try(try_statement, scope, where)
    effect.update(id=name, before=before)
    return effect


def _try(node: t.Node, scope: Scope, where: str) -> dict[str, Any]:
    block = _statements(t.field(node, "tryBlock"))
    if not block:
        raise Unsupported(NETWORK, f"{where}: the try block must await fetch")
    response_name, url, method, headers, body_fields = _fetch_binding(block[0], scope, where)
    position = 1
    checks_ok = False
    if position < len(block) and _is_ok_check(block[position], response_name):
        checks_ok = True
        position += 1
    data_name: str | None = None
    response_type: dict[str, Any] | str | None = None
    if position < len(block):
        parsed = _json_binding(block[position], response_name)
        if parsed is not None:
            data_name, response_type = parsed
            position += 1
    inner = scope.with_data(data_name)
    success: list[dict[str, Any]] = []
    for statement in block[position:]:
        expression = _expression(statement)
        if expression is None:
            raise Unsupported(NETWORK, f"{where}: success statements must be actions")
        success.extend(lower_handler(_arrow(expression), inner))
    catch = t.field(node, "catchClause")
    failure: list[dict[str, Any]] = []
    if catch is not None:
        declaration = t.field(catch, "variableDeclaration")
        if declaration is not None and t.identifier_name(t.field(declaration, "name")) is None:
            raise Unsupported(NETWORK, f"{where}: catch bindings must be plain identifiers")
        failure = _flag_actions(_statements(t.field(catch, "block")), scope, f"{where} catch")
    after = _flag_actions(_statements(t.field(node, "finallyBlock")), scope, f"{where} finally")
    if method == "POST":
        if failure:
            raise Unsupported(
                NETWORK, f"{where}: submissions must not change state when the request fails"
            )
        if data_name is not None:
            raise Unsupported(NETWORK, f"{where}: submissions must not parse the response")
    else:
        if body_fields is not None:
            raise Unsupported(NETWORK, f"{where}: GET requests must not send a body")
        for action in success:
            if "set" not in action or not _uses_data(action["value"]):
                raise Unsupported(
                    NETWORK, f"{where}: loaders must assign the parsed response to state"
                )
        if data_name is None:
            raise Unsupported(NETWORK, f"{where}: loaders must await response.json()")
    for action in success:
        if "run" in action or "store" in action:
            raise Unsupported(NETWORK, f"{where}: effects may not run effects or write storage")
    return {
        "kind": "fetch",
        "method": method,
        "url": url,
        "headers": headers,
        "body": body_fields,
        "checks_ok": checks_ok,
        "success": success,
        "failure": failure,
        "after": after,
        "response": response_type,
    }


def _uses_data(expr: Any) -> bool:
    if isinstance(expr, dict):
        return "data" in expr or any(_uses_data(value) for value in expr.values())
    if isinstance(expr, list):
        return any(_uses_data(item) for item in expr)
    return False


def _fetch_binding(
    statement: t.Node, scope: Scope, where: str
) -> tuple[str, list[Expr], str, dict[str, Expr], dict[str, Expr] | None]:
    declaration = _const_declaration(statement)
    initializer = t.field(declaration, "initializer") if declaration is not None else None
    name = t.identifier_name(t.field(declaration, "name")) if declaration is not None else None
    if declaration is None or name is None or initializer is None:
        raise Unsupported(NETWORK, f"{where}: expected const response = await fetch(...)")
    if t.kind(initializer) != "AwaitExpression":
        raise Unsupported(NETWORK, f"{where}: fetch must be awaited")
    call = t.field(initializer, "expression")
    parts = t.call_parts(call) if call is not None else None
    if parts is None or t.identifier_name(t.unparenthesize(parts[0])) != "fetch":
        raise Unsupported(NETWORK, f"{where}: expected await fetch(url[, init])")
    arguments = parts[1]
    if not 1 <= len(arguments) <= 2:
        raise Unsupported(NETWORK, f"{where}: fetch takes a url and an optional init object")
    url = _url(arguments[0], scope, where)
    method = "GET"
    headers: dict[str, Expr] = {}
    body_fields: dict[str, Expr] | None = None
    if len(arguments) == 2:
        method, headers, body_fields = _init(arguments[1], scope, where)
    return name, url, method, headers, body_fields


def _url(node: t.Node, scope: Scope, where: str) -> list[Expr]:
    lowered = lower(node, scope)
    parts = lowered.get("template", [lowered])
    for part in parts:
        if not ("lit" in part and isinstance(part["lit"], str)) and "param" not in part:
            raise Unsupported(
                NETWORK, f"{where}: urls must be literals or templates over route parameters"
            )
    return list(parts)


def _init(
    node: t.Node, scope: Scope, where: str
) -> tuple[str, dict[str, Expr], dict[str, Expr] | None]:
    node = t.unparenthesize(node)
    if t.kind(node) != "ObjectLiteralExpression":
        raise Unsupported(NETWORK, f"{where}: fetch init must be an object literal")
    method = "GET"
    headers: dict[str, Expr] = {}
    body_fields: dict[str, Expr] | None = None
    seen: set[str] = set()
    for prop in t.field_list(node, "properties"):
        name_node = t.field(prop, "name")
        key = t.identifier_name(name_node) or t.string_value(name_node)
        initializer = t.field(prop, "initializer")
        if (
            t.kind(prop) != "PropertyAssignment"
            or key is None
            or initializer is None
            or key in seen
        ):
            raise Unsupported(NETWORK, f"{where}: fetch init needs literal method, headers, body")
        seen.add(key)
        if key == "method":
            literal = t.string_value(t.unparenthesize(initializer))
            if literal not in METHODS:
                raise Unsupported(NETWORK, f"{where}: method must be GET or POST")
            method = str(literal)
        elif key == "headers":
            lowered = lower(initializer, scope)
            if "object" not in lowered:
                raise Unsupported(NETWORK, f"{where}: headers must be an object literal")
            for header, value in lowered["object"].items():
                _string_template(value, f"{where}: header {header}")
            headers = dict(lowered["object"])
        elif key == "body":
            parts = t.call_parts(t.unparenthesize(initializer))
            if (
                parts is None
                or t.property_chain(parts[0]) != ("JSON", "stringify")
                or len(parts[1]) != 1
            ):
                raise Unsupported(NETWORK, f"{where}: body must be JSON.stringify(object)")
            lowered = lower(parts[1][0], scope)
            if "object" not in lowered:
                raise Unsupported(NETWORK, f"{where}: body must serialize an object literal")
            body_fields = dict(lowered["object"])
        else:
            raise Unsupported(NETWORK, f"{where}: fetch init option {key!r} is not qualified")
    if method == "POST" and body_fields is None:
        raise Unsupported(NETWORK, f"{where}: POST requests need a JSON body")
    return method, headers, body_fields


def _string_template(value: Expr, where: str) -> None:
    parts = value.get("template", [value])
    for part in parts:
        if "lit" in part and isinstance(part["lit"], str):
            continue
        if "ref" in part or "param" in part:
            continue
        raise Unsupported(NETWORK, f"{where} must be a literal or a template over state")


def _const_declaration(statement: t.Node) -> t.Node | None:
    if t.kind(statement) != "VariableStatement":
        return None
    declarations = t.field(statement, "declarationList")
    items = t.field_list(declarations, "declarations") if declarations else []
    if declarations is None or declarations.get("decl") != "const" or len(items) != 1:
        return None
    return items[0]


def _is_ok_check(statement: t.Node, response: str) -> bool:
    if t.kind(statement) != "IfStatement":
        return False
    condition = t.unparenthesize(t.field(statement, "expression") or {})
    operand = t.field(condition, "operand")
    if (
        t.kind(condition) != "PrefixUnaryExpression"
        or condition.get("op") != "ExclamationToken"
        or operand is None
        or t.property_chain(operand) != (response, "ok")
        or t.field(statement, "elseStatement") is not None
    ):
        return False
    then = t.field(statement, "thenStatement")
    statements = _statements(then) if then is not None and t.kind(then) == "Block" else [then]
    if len(statements) != 1 or statements[0] is None:
        return False
    thrown = statements[0]
    if t.kind(thrown) != "ThrowStatement":
        return False
    expression = t.unparenthesize(t.field(thrown, "expression") or {})
    return t.kind(expression) == "NewExpression" and (
        t.identifier_name(t.field(expression, "expression")) == "Error"
    )


def _json_binding(
    statement: t.Node, response: str
) -> tuple[str, dict[str, Any] | str | None] | None:
    declaration = _const_declaration(statement)
    if declaration is None:
        return None
    name = t.identifier_name(t.field(declaration, "name"))
    initializer = t.field(declaration, "initializer")
    if name is None or initializer is None or t.kind(initializer) != "AwaitExpression":
        return None
    call = t.field(initializer, "expression")
    parts = t.call_parts(call) if call is not None else None
    if parts is None or parts[1] or t.property_chain(parts[0]) != (response, "json"):
        return None
    annotation = t.field(declaration, "type")
    return name, type_ir(annotation) if annotation is not None else None


def type_ir(node: t.Node) -> dict[str, Any] | str:
    """Lower a TypeScript type node to the state type IR; unknown shapes are gaps."""
    kind = t.kind(node)
    scalars = {"StringKeyword": "string", "NumberKeyword": "number", "BooleanKeyword": "boolean"}
    if kind in scalars:
        return scalars[kind]
    if kind == "ArrayType":
        element = t.field(node, "elementType")
        if element is not None:
            return {"array": type_ir(element)}
    if kind == "TypeReference" and not t.field_list(node, "typeArguments"):
        name = t.identifier_name(t.field(node, "typeName"))
        if name is not None:
            return {"model": name}
    if kind == "UnionType":
        members = t.field_list(node, "types")
        nulls = [
            item
            for item in members
            if t.kind(item) == "LiteralType"
            and t.kind(t.field(item, "literal") or {}) == "NullKeyword"
        ]
        if len(members) == 2 and len(nulls) == 1:
            other = next(item for item in members if item is not nulls[0])
            return {"nullable": type_ir(other)}
    if kind == "ParenthesizedType":
        inner = t.field(node, "type")
        if inner is not None:
            return type_ir(inner)
    raise Unsupported(
        "SANKA_RN_UNSUPPORTED_HOOK",
        "types must be string, number, boolean, a model interface, T[] or T | null",
    )


def capture_use_effect(
    call: t.Node, scope: Scope, rel: str, effects: dict[str, dict[str, Any]]
) -> list[str]:
    """Lower ``useEffect(() => {...}, [])``; return the effect ids it runs on appear."""
    parts = t.call_parts(call)
    if parts is None or len(parts[1]) != 2:
        raise Unsupported(
            NETWORK, f"{rel}: useEffect takes a callback and an empty dependency list"
        )
    callback, dependencies = (t.unparenthesize(item) for item in parts[1])
    if t.kind(dependencies) != "ArrayLiteralExpression" or t.field_list(dependencies, "elements"):
        raise Unsupported(NETWORK, f"{rel}: only mount effects with [] dependencies are qualified")
    if t.kind(callback) != "ArrowFunction" or t.modifier_kinds(callback):
        raise Unsupported(NETWORK, f"{rel}: useEffect callbacks must be plain arrow functions")
    if t.field_list(callback, "parameters"):
        raise Unsupported(NETWORK, f"{rel}: useEffect callbacks take no parameters")
    body = t.field(callback, "body")
    statements = _statements(body) if body is not None and t.kind(body) == "Block" else [body]
    appear: list[str] = []
    for statement in statements:
        if statement is None:
            raise Unsupported(NETWORK, f"{rel}: malformed useEffect callback")
        expression = _expression(statement) if t.kind(statement) != "Block" else None
        if expression is None and t.kind(statement) not in {"ExpressionStatement"}:
            expression = statement if t.kind(statement) != "Block" else None
        if expression is None:
            raise Unsupported(NETWORK, f"{rel}: useEffect statements must be effect calls")
        expression = t.unparenthesize(expression)
        parts_call = t.call_parts(expression)
        if parts_call is None:
            raise Unsupported(NETWORK, f"{rel}: useEffect statements must be effect calls")
        callee = t.unparenthesize(parts_call[0])
        name = t.identifier_name(callee)
        if name is not None:
            if name not in scope.effects or parts_call[1]:
                raise Unsupported(NETWORK, f"{rel}: useEffect may only call declared effects")
            appear.append(name)
            continue
        effect = _chain(expression, scope, rel)
        if effect["id"] in effects:
            raise Unsupported(NETWORK, f"{rel}: effect id {effect['id']!r} is already declared")
        effects[effect["id"]] = effect
        appear.append(effect["id"])
    return appear


def _chain(expression: t.Node, scope: Scope, rel: str) -> dict[str, Any]:
    """Lower ``fetch(url).then(r => r.json()).then(setX)[.catch(...)]`` or a storage read."""
    calls: list[tuple[str, list[t.Node]]] = []
    node = expression
    while True:
        parts = t.call_parts(node)
        if parts is None:
            raise Unsupported(NETWORK, f"{rel}: useEffect statements must be promise chains")
        callee = t.unparenthesize(parts[0])
        if t.kind(callee) == "PropertyAccessExpression":
            method = t.identifier_name(t.field(callee, "name"))
            target = t.field(callee, "expression")
            if method in {"then", "catch"} and target is not None:
                calls.insert(0, (method, parts[1]))
                node = t.unparenthesize(target)
                continue
        calls.insert(0, ("head", parts[1]))
        head = callee
        break
    chain = t.property_chain(head)
    if chain is not None and scope.storage is not None and chain == (scope.storage, "getItem"):
        return _storage_read(calls, scope, rel)
    if t.identifier_name(head) == "fetch":
        return _anonymous_loader(calls, scope, rel)
    raise Unsupported(NETWORK, f"{rel}: useEffect may only fetch or read AsyncStorage")


def _storage_read(calls: list[tuple[str, list[t.Node]]], scope: Scope, rel: str) -> dict[str, Any]:
    head_arguments = calls[0][1]
    key = t.string_value(t.unparenthesize(head_arguments[0])) if len(head_arguments) == 1 else None
    if key is None:
        raise Unsupported(STORAGE, f"{rel}: AsyncStorage.getItem needs one literal key")
    if len(calls) != 2 or calls[1][0] != "then" or len(calls[1][1]) != 1:
        raise Unsupported(STORAGE, f"{rel}: AsyncStorage.getItem must be followed by one .then")
    actions = _then_actions(calls[1][1][0], scope, rel, STORAGE)
    for action in actions:
        if "set" not in action or not _uses_data(action["value"]):
            raise Unsupported(STORAGE, f"{rel}: storage reads must assign the value to state")
    return {"id": f"read-{key}", "kind": "storage-read", "key": key, "success": actions}


def _anonymous_loader(
    calls: list[tuple[str, list[t.Node]]], scope: Scope, rel: str
) -> dict[str, Any]:
    head_arguments = calls[0][1]
    if len(head_arguments) != 1:
        raise Unsupported(NETWORK, f"{rel}: chained fetch takes only a url")
    url = _url(head_arguments[0], scope, f"{rel}: effect {ANONYMOUS_LOADER}")
    rest = calls[1:]
    if len(rest) < 2 or rest[0][0] != "then" or rest[1][0] != "then":
        raise Unsupported(NETWORK, f"{rel}: chained fetch needs .then(r => r.json()).then(setX)")
    if not _is_json_then(rest[0][1]):
        raise Unsupported(NETWORK, f"{rel}: the first .then must return response.json()")
    if len(rest[1][1]) != 1:
        raise Unsupported(NETWORK, f"{rel}: the second .then takes one handler")
    success = _then_actions(rest[1][1][0], scope, rel, NETWORK)
    failure: list[dict[str, Any]] = []
    if len(rest) == 3:
        if rest[2][0] != "catch" or len(rest[2][1]) != 1:
            raise Unsupported(NETWORK, f"{rel}: chained fetch may end with one .catch")
        handler = t.unparenthesize(rest[2][1][0])
        if t.kind(handler) != "ArrowFunction":
            raise Unsupported(NETWORK, f"{rel}: .catch handlers must be arrow functions")
        body = t.field(handler, "body")
        statements = _statements(body) if body is not None and t.kind(body) == "Block" else []
        if body is not None and t.kind(body) != "Block":
            statements = [{"k": "ExpressionStatement", "s": 0, "e": 0, "f": {"expression": body}}]
        failure = _flag_actions(statements, scope, f"{rel}: effect {ANONYMOUS_LOADER} catch")
    elif len(rest) > 3:
        raise Unsupported(NETWORK, f"{rel}: chained fetch may end with one .catch")
    for action in success:
        if "set" not in action or not _uses_data(action["value"]):
            raise Unsupported(NETWORK, f"{rel}: loaders must assign the parsed response to state")
    return {
        "id": ANONYMOUS_LOADER,
        "kind": "fetch",
        "method": "GET",
        "url": url,
        "headers": {},
        "body": None,
        "checks_ok": False,
        "before": [],
        "success": success,
        "failure": failure,
        "after": [],
        "response": None,
    }


def _is_json_then(arguments: list[t.Node]) -> bool:
    if len(arguments) != 1:
        return False
    handler = t.unparenthesize(arguments[0])
    parameters = t.field_list(handler, "parameters")
    name = t.identifier_name(t.field(parameters[0], "name")) if len(parameters) == 1 else None
    body = t.field(handler, "body")
    if t.kind(handler) != "ArrowFunction" or name is None or body is None:
        return False
    if t.kind(body) == "Block":
        statements = _statements(body)
        if len(statements) != 1 or t.kind(statements[0]) != "ReturnStatement":
            return False
        body = t.field(statements[0], "expression") or {}
    parts = t.call_parts(t.unparenthesize(body))
    return parts is not None and not parts[1] and t.property_chain(parts[0]) == (name, "json")


def _then_actions(handler: t.Node, scope: Scope, rel: str, code: str) -> list[dict[str, Any]]:
    """Lower ``setX`` or ``(value) => setX(...)`` receiving the resolved value as data."""
    handler = t.unparenthesize(handler)
    if t.kind(handler) == "Identifier":
        setter = t.text(handler) or ""
        if setter not in scope.setters:
            raise Unsupported(code, f"{rel}: .then handlers must be setters or arrow functions")
        return [{"set": scope.setters[setter], "value": {"data": []}}]
    if t.kind(handler) != "ArrowFunction" or t.modifier_kinds(handler):
        raise Unsupported(code, f"{rel}: .then handlers must be setters or arrow functions")
    parameters = t.field_list(handler, "parameters")
    name = t.identifier_name(t.field(parameters[0], "name")) if len(parameters) == 1 else None
    if name is None:
        raise Unsupported(code, f"{rel}: .then handlers take one parameter")
    inner = scope.with_data(name)
    return lower_handler(
        {
            "k": "ArrowFunction",
            "s": 0,
            "e": 0,
            "f": {"parameters": [], "body": t.field(handler, "body")},
        },
        inner,
    )

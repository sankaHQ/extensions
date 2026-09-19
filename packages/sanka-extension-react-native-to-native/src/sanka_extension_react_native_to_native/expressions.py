# SPDX-License-Identifier: Apache-2.0
"""Bounded lowering of screen expressions and event handlers to a JSON IR.

Everything outside the recognized grammar raises ``Unsupported`` with a stable
reason code. Nothing is evaluated; the IR records what the source says.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from sanka_ts_capture import tree as t

Expr = dict[str, Any]

BINARY = {
    "PlusToken": "+",
    "MinusToken": "-",
    "EqualsEqualsEqualsToken": "===",
    "ExclamationEqualsEqualsToken": "!==",
    "GreaterThanToken": ">",
    "LessThanToken": "<",
    "GreaterThanEqualsToken": ">=",
    "LessThanEqualsToken": "<=",
    "AmpersandAmpersandToken": "&&",
    "BarBarToken": "||",
}
MAX_ACTIONS = 3


class Unsupported(ValueError):
    """A construct outside the slice-1 envelope, with a stable reason code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Scope:
    """Symbols a screen expression may reference."""

    state: dict[str, str]
    setters: dict[str, str]
    params: frozenset[str]
    route_object: str | None
    navigation: str | None
    navigation_kind: str
    styles: str | None
    item: str | None = None
    args: tuple[str, ...] = ()
    locals: frozenset[str] = field(default_factory=frozenset)

    def with_item(self, name: str) -> Scope:
        return replace(self, item=name)

    def with_args(self, names: tuple[str, ...]) -> Scope:
        return replace(self, args=names)


def number(text: str) -> int | float:
    if text.isdigit():
        return int(text)
    try:
        return float(text)
    except ValueError as error:
        raise Unsupported("SANKA_RN_EXPRESSION", f"unsupported numeric literal {text!r}") from error


def is_literal(expr: Expr) -> bool:
    """True when the IR contains only literals, arrays and objects of literals."""
    if "lit" in expr:
        return True
    if "array" in expr:
        return all(is_literal(item) for item in expr["array"])
    if "object" in expr:
        return all(is_literal(item) for item in expr["object"].values())
    return False


def lower(node: t.Node, scope: Scope) -> Expr:
    node = t.unparenthesize(node)
    kind = t.kind(node)
    if kind in {"StringLiteral", "NoSubstitutionTemplateLiteral"}:
        return {"lit": t.text(node)}
    if kind == "NumericLiteral":
        return {"lit": number(t.text(node) or "")}
    if kind == "TrueKeyword":
        return {"lit": True}
    if kind == "FalseKeyword":
        return {"lit": False}
    if kind == "NullKeyword":
        return {"lit": None}
    if kind == "Identifier":
        return _reference((t.text(node) or "",), scope)
    if kind == "PropertyAccessExpression":
        chain = t.property_chain(node)
        if chain is None:
            raise Unsupported("SANKA_RN_EXPRESSION", "optional or private member access")
        if len(chain) >= 2 and chain[-1] == "length":
            return {"length": _reference(chain[:-1], scope)}
        return _reference(chain, scope)
    if kind == "TemplateExpression":
        parts: list[Expr] = []
        head = t.field(node, "head")
        if head is not None and t.text(head):
            parts.append({"lit": t.text(head)})
        for span in t.field_list(node, "templateSpans"):
            expression = t.field(span, "expression")
            literal = t.field(span, "literal")
            if expression is None or literal is None:
                raise Unsupported("SANKA_RN_EXPRESSION", "malformed template literal")
            parts.append(lower(expression, scope))
            if t.text(literal):
                parts.append({"lit": t.text(literal)})
        return {"template": parts}
    if kind == "CallExpression":
        parts_call = t.call_parts(node)
        if (
            parts_call is not None
            and t.identifier_name(t.unparenthesize(parts_call[0])) == "String"
            and len(parts_call[1]) == 1
        ):
            return {"string": lower(parts_call[1][0], scope)}
        raise Unsupported(
            "SANKA_RN_EXPRESSION", "function calls other than String(value) are not qualified"
        )
    if kind == "PrefixUnaryExpression":
        operand = t.field(node, "operand")
        if operand is None:
            raise Unsupported("SANKA_RN_EXPRESSION", "malformed unary expression")
        if node.get("op") == "ExclamationToken":
            return {"not": lower(operand, scope)}
        if node.get("op") == "MinusToken" and t.kind(t.unparenthesize(operand)) == "NumericLiteral":
            value = number(t.text(t.unparenthesize(operand)) or "")
            return {"lit": -value}
        raise Unsupported("SANKA_RN_EXPRESSION", "unsupported unary operator")
    if kind == "BinaryExpression":
        operator = t.field(node, "operatorToken")
        left = t.field(node, "left")
        right = t.field(node, "right")
        if operator is None or left is None or right is None:
            raise Unsupported("SANKA_RN_EXPRESSION", "malformed binary expression")
        symbol = BINARY.get(t.kind(operator))
        if symbol is None:
            raise Unsupported(
                "SANKA_RN_EXPRESSION", f"operator {t.kind(operator)} is not qualified"
            )
        return {"binary": symbol, "left": lower(left, scope), "right": lower(right, scope)}
    if kind == "ConditionalExpression":
        condition = t.field(node, "condition")
        when_true = t.field(node, "whenTrue")
        when_false = t.field(node, "whenFalse")
        if condition is None or when_true is None or when_false is None:
            raise Unsupported("SANKA_RN_EXPRESSION", "malformed conditional expression")
        return {
            "cond": lower(condition, scope),
            "then": lower(when_true, scope),
            "else": lower(when_false, scope),
        }
    if kind == "ArrayLiteralExpression":
        elements = t.field_list(node, "elements")
        if elements and t.kind(elements[0]) == "SpreadElement":
            spread = t.field(elements[0], "expression")
            if spread is None or len(elements) != 2 or t.kind(elements[1]) == "SpreadElement":
                raise Unsupported(
                    "SANKA_RN_EXPRESSION", "only [...state, value] array appends are qualified"
                )
            base = lower(spread, scope)
            if "ref" not in base:
                raise Unsupported("SANKA_RN_EXPRESSION", "array appends must spread a state value")
            return {"append": base, "value": lower(elements[1], scope)}
        if any(t.kind(item) in {"SpreadElement", "OmittedExpression"} for item in elements):
            raise Unsupported("SANKA_RN_EXPRESSION", "array spreads and holes are not qualified")
        return {"array": [lower(item, scope) for item in elements]}
    if kind == "ObjectLiteralExpression":
        values: dict[str, Expr] = {}
        for prop in t.field_list(node, "properties"):
            prop_kind = t.kind(prop)
            name_node = t.field(prop, "name")
            key = t.identifier_name(name_node) or t.string_value(name_node)
            if key is None or key in values:
                raise Unsupported("SANKA_RN_EXPRESSION", "object keys must be unique identifiers")
            if prop_kind == "PropertyAssignment":
                initializer = t.field(prop, "initializer")
                if initializer is None:
                    raise Unsupported("SANKA_RN_EXPRESSION", "malformed object property")
                values[key] = lower(initializer, scope)
            elif prop_kind == "ShorthandPropertyAssignment":
                values[key] = _reference((key,), scope)
            else:
                raise Unsupported(
                    "SANKA_RN_EXPRESSION", "object spreads and methods are not qualified"
                )
        return {"object": values}
    if kind in {"AsExpression", "SatisfiesExpression", "TypeAssertionExpression"}:
        raise Unsupported("SANKA_RN_EXPRESSION", "type assertions require additional capture")
    if kind == "NonNullExpression":
        raise Unsupported("SANKA_RN_EXPRESSION", "non-null assertions require additional capture")
    raise Unsupported("SANKA_RN_EXPRESSION", f"{kind} expressions require additional capture")


def _reference(chain: tuple[str, ...], scope: Scope) -> Expr:
    head, rest = chain[0], list(chain[1:])
    if scope.route_object is not None and head == scope.route_object:
        if len(chain) == 3 and chain[1] == "params" and chain[2] in scope.params:
            return {"param": chain[2]}
        raise Unsupported(
            "SANKA_RN_PARAM", f"'{'.'.join(chain)}' is not a declared route parameter"
        )
    if head in scope.state:
        return {"ref": [head, *rest]}
    if head in scope.params and not rest:
        return {"param": head}
    if scope.item is not None and head == scope.item:
        return {"item": rest}
    if head in scope.args and not rest:
        return {"arg": scope.args.index(head)}
    if head in scope.locals and not rest:
        return {"local": head}
    if head in scope.setters or head == scope.navigation or head == scope.styles:
        raise Unsupported("SANKA_RN_EXPRESSION", f"'{'.'.join(chain)}' cannot be used as a value")
    raise Unsupported(
        "SANKA_RN_SYMBOL",
        f"'{'.'.join(chain)}' is not a state value, route parameter, list item or handler argument",
    )


def lower_handler(node: t.Node, scope: Scope) -> list[dict[str, Any]]:
    """Lower an event prop value to a bounded list of actions."""
    node = t.unparenthesize(node)
    kind = t.kind(node)
    if kind == "Identifier":
        name = t.text(node) or ""
        if name in scope.setters:
            return [{"set": scope.setters[name], "value": {"arg": 0}}]
        raise Unsupported("SANKA_RN_EVENT", f"handler '{name}' is not a state setter")
    if kind == "PropertyAccessExpression":
        chain = t.property_chain(node)
        if (
            chain is not None
            and scope.navigation is not None
            and chain
            == (
                scope.navigation,
                "goBack" if scope.navigation_kind == "react-navigation" else "back",
            )
        ):
            return [{"back": True}]
        raise Unsupported("SANKA_RN_EVENT", "only bound setters and back navigation are qualified")
    if kind not in {"ArrowFunction", "FunctionExpression"}:
        raise Unsupported("SANKA_RN_EVENT", "event handlers must be inline functions or setters")
    if t.modifier_kinds(node):
        raise Unsupported("SANKA_RN_EVENT", "async or decorated handlers are not qualified")
    names: list[str] = []
    for parameter in t.field_list(node, "parameters"):
        parameter_name = t.identifier_name(t.field(parameter, "name"))
        if parameter_name is None or t.field(parameter, "initializer") is not None:
            raise Unsupported("SANKA_RN_EVENT", "handler parameters must be plain identifiers")
        names.append(parameter_name)
    inner = scope.with_args(tuple(names))
    body = t.field(node, "body")
    if body is None:
        raise Unsupported("SANKA_RN_EVENT", "malformed handler")
    if t.kind(body) == "Block":
        statements = t.field_list(body, "statements")
        if not 1 <= len(statements) <= MAX_ACTIONS:
            raise Unsupported("SANKA_RN_EVENT", f"handlers may contain 1 to {MAX_ACTIONS} actions")
        actions = []
        for statement in statements:
            expression = t.field(statement, "expression")
            if t.kind(statement) != "ExpressionStatement" or expression is None:
                raise Unsupported("SANKA_RN_EVENT", "handler statements must be actions")
            actions.append(_action(expression, inner))
        return actions
    return [_action(body, inner)]


def _action(node: t.Node, scope: Scope) -> dict[str, Any]:
    parts = t.call_parts(node)
    if parts is None:
        raise Unsupported("SANKA_RN_EVENT", "actions must be navigation or state setter calls")
    callee, arguments = parts
    chain = t.property_chain(callee)
    if chain is not None and len(chain) == 1 and chain[0] in scope.setters:
        if len(arguments) != 1:
            raise Unsupported("SANKA_RN_EVENT", "state setters take exactly one value")
        return {"set": scope.setters[chain[0]], "value": lower(arguments[0], scope)}
    if chain is None or scope.navigation is None or chain[0] != scope.navigation or len(chain) != 2:
        raise Unsupported("SANKA_RN_EVENT", "actions must be navigation or state setter calls")
    method = chain[1]
    if scope.navigation_kind == "react-navigation":
        if method == "goBack" and not arguments:
            return {"back": True}
        if method == "navigate" and 1 <= len(arguments) <= 2:
            name = t.string_value(t.unparenthesize(arguments[0]))
            if name is None:
                raise Unsupported("SANKA_RN_EVENT", "navigate requires a literal screen name")
            params = _params(arguments[1], scope) if len(arguments) == 2 else {}
            return {"navigate": name, "params": params}
        raise Unsupported("SANKA_RN_EVENT", f"navigation.{method} requires additional capture")
    if method == "back" and not arguments:
        return {"back": True}
    if method == "push" and len(arguments) == 1:
        return {"push": _href(arguments[0], scope)[0], "params": _href(arguments[0], scope)[1]}
    raise Unsupported("SANKA_RN_EVENT", f"router.{method} requires additional capture")


def _params(node: t.Node, scope: Scope) -> dict[str, Expr]:
    value = lower(node, scope)
    if "object" not in value:
        raise Unsupported("SANKA_RN_EVENT", "navigation parameters must be an object literal")
    params: dict[str, Expr] = value["object"]
    return params


def href(node: t.Node, scope: Scope) -> tuple[str, dict[str, Expr]]:
    """Lower an Expo Router href: a literal path or ``{ pathname, params }``."""
    return _href(node, scope)


def _href(node: t.Node, scope: Scope) -> tuple[str, dict[str, Expr]]:
    node = t.unparenthesize(node)
    literal = t.string_value(node)
    if literal is not None:
        if not literal.startswith("/"):
            raise Unsupported("SANKA_RN_EXPO_DYNAMIC_ROUTE", "hrefs must be absolute literal paths")
        return literal, {}
    value = lower(node, scope)
    if "object" not in value:
        raise Unsupported("SANKA_RN_EVENT", "router targets must be a literal path or object")
    fields: dict[str, Expr] = value["object"]
    pathname = fields.get("pathname")
    if set(fields) - {"pathname", "params"} or pathname is None or "lit" not in pathname:
        raise Unsupported("SANKA_RN_EVENT", "router objects need a literal pathname and params")
    params = fields.get("params", {"object": {}})
    if "object" not in params:
        raise Unsupported("SANKA_RN_EVENT", "router params must be an object literal")
    path = str(pathname["lit"])
    if not path.startswith("/"):
        raise Unsupported("SANKA_RN_EXPO_DYNAMIC_ROUTE", "hrefs must be absolute literal paths")
    return path, dict(params["object"])

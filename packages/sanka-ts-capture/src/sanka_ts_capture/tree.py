# SPDX-License-Identifier: Apache-2.0
"""Helpers over the driver's JSON syntax tree.

Nodes are plain dictionaries: ``k`` is the TypeScript SyntaxKind name, ``s`` and
``e`` are character offsets, ``t`` holds identifier or literal text, ``f`` maps
TypeScript property names to child nodes or node lists, ``op`` names a unary
operator, ``decl`` records a variable list's declaration keyword, ``typeOnly`` and
``exportEquals`` mirror the TypeScript flags, and ``json`` carries the exact
JavaScript serialization of a pure literal object or array.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

Node = dict[str, Any]


def kind(node: Node) -> str:
    return str(node.get("k", ""))


def fields(node: Node) -> dict[str, Any]:
    value = node.get("f")
    return value if isinstance(value, dict) else {}


def field(node: Node, name: str) -> Node | None:
    value = fields(node).get(name)
    return value if isinstance(value, dict) else None


def field_list(node: Node, name: str) -> list[Node]:
    value = fields(node).get(name)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def text(node: Node) -> str | None:
    value = node.get("t")
    return value if isinstance(value, str) else None


def start(node: Node) -> int:
    value = node.get("s")
    return value if isinstance(value, int) else 0


def line_of(source: str, node: Node) -> int:
    return source.count("\n", 0, start(node)) + 1


def modifier_kinds(node: Node) -> set[str]:
    return {kind(item) for item in field_list(node, "modifiers")}


def unparenthesize(node: Node) -> Node:
    while kind(node) == "ParenthesizedExpression":
        inner = field(node, "expression")
        if inner is None:
            break
        node = inner
    return node


def identifier_name(node: Node | None) -> str | None:
    if node is not None and kind(node) == "Identifier":
        return text(node)
    return None


def string_value(node: Node | None) -> str | None:
    if node is not None and kind(node) in {"StringLiteral", "NoSubstitutionTemplateLiteral"}:
        return text(node)
    return None


def property_chain(node: Node) -> tuple[str, ...] | None:
    """Return ``("a", "b", "c")`` for ``a.b.c``; ``None`` for anything else."""
    node = unparenthesize(node)
    if kind(node) == "Identifier":
        name = text(node)
        return (name,) if name else None
    if kind(node) == "PropertyAccessExpression" and field(node, "questionDotToken") is None:
        base = field(node, "expression")
        name = identifier_name(field(node, "name"))
        if base is None or name is None:
            return None
        prefix = property_chain(base)
        return (*prefix, name) if prefix else None
    return None


def call_parts(node: Node, *, allow_type_arguments: bool = False) -> tuple[Node, list[Node]] | None:
    """Return ``(callee, arguments)`` for a plain call; type arguments only when allowed."""
    node = unparenthesize(node)
    if (
        kind(node) != "CallExpression"
        or field(node, "questionDotToken") is not None
        or (field_list(node, "typeArguments") and not allow_type_arguments)
    ):
        return None
    callee = field(node, "expression")
    if callee is None:
        return None
    return callee, field_list(node, "arguments")


def walk(node: Node) -> Iterator[Node]:
    yield node
    for value in fields(node).values():
        if isinstance(value, dict):
            yield from walk(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield from walk(item)

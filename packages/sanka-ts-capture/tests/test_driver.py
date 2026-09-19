# SPDX-License-Identifier: Apache-2.0
"""The vendored TypeScript driver parses and transpiles without touching other files."""

from __future__ import annotations

import pytest

from sanka_ts_capture import (
    MIN_NODE_MAJOR,
    TypeScriptDriverError,
    bundle_path,
    node_executable,
    node_version,
    parse_sources,
    transpile_sources,
    tree,
)


def _node_ready() -> bool:
    try:
        return node_version(node_executable())[0] >= MIN_NODE_MAJOR
    except TypeScriptDriverError:
        return False


needs_node = pytest.mark.skipif(not _node_ready(), reason="requires Node.js 20 or later")


def test_bundle_digest_is_pinned() -> None:
    assert bundle_path().name == "typescript.js"


def test_request_limits_are_enforced() -> None:
    with pytest.raises(TypeScriptDriverError):
        parse_sources({})
    with pytest.raises(TypeScriptDriverError):
        parse_sources({"big.ts": "x" * (512 * 1024 + 1)})


@needs_node
def test_parse_tree_literals_and_helpers() -> None:
    source = (
        'import express from "express";\n'
        "const payload = { a: 1, 'b': [true, null, -2.5, 9223372036854775809], c: \"s\" };\n"
        "app.get('/x', (req, res) => res.status(201).json(payload));\n"
    )
    parsed = parse_sources({"src/app.ts": source})["src/app.ts"]
    assert parsed.script_kind == "ts"
    assert parsed.diagnostics == ()
    statements = tree.field_list(parsed.tree, "statements")
    assert [tree.kind(item) for item in statements] == [
        "ImportDeclaration",
        "VariableStatement",
        "ExpressionStatement",
    ]
    declarations = tree.field(statements[1], "declarationList")
    assert declarations is not None and declarations["decl"] == "const"
    payload = tree.field(tree.field_list(declarations, "declarations")[0], "initializer")
    assert payload is not None
    # JavaScript number semantics decide the serialization, not Python's.
    assert payload["json"] == '{"a":1,"b":[true,null,-2.5,9223372036854776000],"c":"s"}'
    call = tree.call_parts(tree.field(statements[2], "expression") or {})
    assert call is not None
    callee, arguments = call
    assert tree.property_chain(callee) == ("app", "get")
    assert tree.string_value(arguments[0]) == "/x"
    handler = arguments[1]
    assert tree.kind(handler) == "ArrowFunction"
    parameters = tree.field_list(handler, "parameters")
    assert [tree.identifier_name(tree.field(item, "name")) for item in parameters] == ["req", "res"]
    body = tree.field(handler, "body")
    assert body is not None
    inner = tree.call_parts(body)
    assert inner is not None
    assert tree.identifier_name(tree.field(inner[0], "name")) == "json"
    assert tree.line_of(source, statements[2]) == 3
    assert sum(1 for _ in tree.walk(parsed.tree)) > 20


@needs_node
def test_syntax_errors_are_reported_not_raised() -> None:
    parsed = parse_sources({"broken.ts": "const = ;"})["broken.ts"]
    assert parsed.diagnostics
    assert parsed.diagnostics[0].code > 0


@needs_node
def test_non_literal_payloads_have_no_json() -> None:
    parsed = parse_sources({"a.ts": "const v = { a: f(), ...rest };"})["a.ts"]
    statement = tree.field_list(parsed.tree, "statements")[0]
    declarations = tree.field(statement, "declarationList")
    assert declarations is not None
    value = tree.field(tree.field_list(declarations, "declarations")[0], "initializer")
    assert value is not None and "json" not in value


@needs_node
def test_transpile_to_commonjs() -> None:
    text = 'import express from "express";\nconst app = express();\nexport default app;\n'
    output = transpile_sources({"src/app.ts": text})["src/app.ts"]
    assert 'require("express")' in output
    assert "exports.default" in output
    with pytest.raises(TypeScriptDriverError):
        transpile_sources({"broken.ts": "const = ;"})
    with pytest.raises(TypeScriptDriverError):
        parse_sources({"notes.txt": "hello"})


@needs_node
def test_transpile_tsx_uses_the_classic_react_runtime() -> None:
    # Screen replays rely on this: JSX lowers to React.createElement, so a React binding
    # (import or global) must be in scope, and type-only imports disappear.
    text = (
        'import type { Props } from "./types";\n'
        'import { useState } from "react";\n'
        "export default function Screen(_props: Props) {\n"
        '  const [value] = useState("x");\n'
        "  return <View style={{ flex: 1 }}>{value}</View>;\n"
        "}\n"
    )
    output = transpile_sources({"app/Screen.tsx": text})["app/Screen.tsx"]
    assert "React.createElement" in output
    assert 'require("react")' in output
    assert "./types" not in output
    assert "exports.default = Screen" in output

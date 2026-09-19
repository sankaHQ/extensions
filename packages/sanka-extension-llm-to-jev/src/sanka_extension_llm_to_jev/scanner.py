# SPDX-License-Identifier: Apache-2.0
"""Fail-closed AST inventory; never imports or executes the source application."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from .common import object_digest, safe_path, source_files


def _same(node: ast.AST | None, text: str) -> bool:
    return node is not None and ast.dump(node) == ast.dump(ast.parse(text, mode="eval").body)


def _literal(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError) as error:
        raise ValueError("dynamic request/schema requires manual review") from error


def _supported(
    tree: ast.Module, function: ast.AST | None, call: ast.Call, source: str
) -> dict[str, Any]:
    if not isinstance(function, ast.FunctionDef) or function not in tree.body:
        raise ValueError("only module-level synchronous functions are supported")
    args = function.args
    if (
        function.decorator_list
        or function.type_params
        or args.posonlyargs
        or args.kwonlyargs
        or args.vararg
        or args.kwarg
        or args.defaults
        or len(args.args) != 1
        or not _same(args.args[0].annotation, "str")
        or not _same(function.returns, "str")
    ):
        raise ValueError("requires one annotated str argument and a str return")
    argument = args.args[0].arg
    imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    if not any(
        isinstance(node, ast.ImportFrom)
        and node.module == "openai"
        and node.level == 0
        and len(node.names) == 1
        and node.names[0].name == "OpenAI"
        and node.names[0].asname is None
        for node in imports
    ) or not any(
        isinstance(node, ast.Import)
        and len(node.names) == 1
        and node.names[0].name == "json"
        and node.names[0].asname is None
        for node in imports
    ):
        raise ValueError("canonical unaliased OpenAI and json imports required")
    if not _same(call.func, "client.responses.create"):
        raise ValueError("only direct client.responses.create is supported")
    constructors = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "client"
        and isinstance(node.value, ast.Call)
        and _same(node.value.func, "OpenAI")
    ]
    if len(constructors) != 1:
        raise ValueError("one direct module-level client = OpenAI(...) required")
    constructor = constructors[0]
    assert isinstance(constructor.value, ast.Call)
    if constructor.value.args or any(
        keyword.arg not in {"max_retries", "timeout"}
        or not isinstance(keyword.value, ast.Constant)
        or type(keyword.value.value) not in {int, float}
        for keyword in constructor.value.keywords
    ):
        raise ValueError("client constructor supports only literal timeout/max_retries keywords")
    reserved = {"client", "OpenAI", "json", "Exception", "str"}
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name in reserved
        ):
            raise ValueError("SDK or builtin symbol shadowing requires manual review")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                canonical_import = (
                    isinstance(node, ast.Import) and alias.name == "json" and alias.asname is None
                ) or (
                    isinstance(node, ast.ImportFrom)
                    and node.module == "openai"
                    and node.level == 0
                    and alias.name == "OpenAI"
                    and alias.asname is None
                )
                if bound in reserved and not canonical_import:
                    raise ValueError("SDK or builtin import shadowing requires manual review")
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, (ast.Store, ast.Del))
            and node.id in reserved
            and node is not constructor.targets[0]
        ):
            raise ValueError("client or SDK name rebinding requires manual review")
        if isinstance(node, ast.arg) and node.arg in reserved:
            raise ValueError("SDK name shadowing requires manual review")
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, (ast.Store, ast.Del))
            and any(
                isinstance(child, ast.Name) and child.id in reserved for child in ast.walk(node)
            )
        ):
            raise ValueError("SDK attribute mutation requires manual review")
    if len(function.body) != 1 or not isinstance(function.body[0], ast.Try):
        raise ValueError("requires reviewed try/except returning the existing unknown label")
    attempt = function.body[0]
    if len(attempt.body) != 2 or len(attempt.handlers) != 1 or attempt.orelse or attempt.finalbody:
        raise ValueError("unsupported control flow/result consumer")
    assignment, returned = attempt.body
    if (
        not isinstance(assignment, ast.Assign)
        or len(assignment.targets) != 1
        or not isinstance(assignment.targets[0], ast.Name)
        or assignment.value is not call
        or not isinstance(returned, ast.Return)
    ):
        raise ValueError("expected response assignment followed by direct JSON enum return")
    handler = attempt.handlers[0]
    if (
        not _same(handler.type, "Exception")
        or handler.name
        or len(handler.body) != 1
        or not isinstance(handler.body[0], ast.Return)
        or not isinstance(handler.body[0].value, ast.Constant)
        or not isinstance(handler.body[0].value.value, str)
    ):
        raise ValueError("only except Exception: return <unknown label> is supported")
    unknown = handler.body[0].value.value
    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
    if (
        call.args
        or len(keywords) != len(call.keywords)
        or set(keywords) != {"model", "instructions", "input", "text"}
    ):
        raise ValueError("only fixed model/instructions/input/text request keywords are supported")
    if not _same(keywords["input"], argument):
        raise ValueError("input must be the unmodified text argument")
    model = _literal(keywords["model"])
    prompt = _literal(keywords["instructions"])
    text = _literal(keywords["text"])
    if not isinstance(model, str) or not model or not isinstance(prompt, str) or not prompt:
        raise ValueError("model and instructions must be nonempty string literals")
    if not isinstance(text, dict) or set(text) != {"format"}:
        raise ValueError("requires a single inline text.format")
    form = text["format"]
    if (
        not isinstance(form, dict)
        or set(form) != {"type", "name", "strict", "schema"}
        or form["type"] != "json_schema"
        or form["strict"] is not True
        or not isinstance(form["name"], str)
    ):
        raise ValueError("requires strict fixed JSON schema output")
    schema = form["schema"]
    if (
        not isinstance(schema, dict)
        or set(schema) != {"type", "properties", "required", "additionalProperties"}
        or schema["type"] != "object"
        or schema["additionalProperties"] is not False
        or not isinstance(schema["properties"], dict)
        or len(schema["properties"]) != 1
    ):
        raise ValueError("requires exactly one enum string field")
    field = next(iter(schema["properties"]))
    prop = schema["properties"][field]
    if (
        not isinstance(prop, dict)
        or set(prop) != {"type", "enum"}
        or prop["type"] != "string"
        or schema["required"] != [field]
        or not isinstance(prop["enum"], list)
        or len(prop["enum"]) < 2
        or any(not isinstance(label, str) or not label for label in prop["enum"])
        or len(set(prop["enum"])) != len(prop["enum"])
        or unknown not in prop["enum"]
    ):
        raise ValueError("invalid fixed enum or source fallback is outside the return contract")
    response_name = assignment.targets[0].id
    expected = f"json.loads({response_name}.output_text)[{field!r}]"
    if not _same(returned.value, expected):
        raise ValueError("unsupported response parsing/return consumer")
    return {
        "input_name": argument,
        "enum_field": field,
        "labels": prop["enum"],
        "unknown_label": unknown,
        "prompt": prompt,
        "model": model,
        "consumer": ast.get_source_segment(source, returned),
    }


def scan_project(root: Path) -> dict[str, Any]:
    files = source_files(root)
    sites: list[dict[str, Any]] = []
    for relative, source_hash in files.items():
        if not relative.endswith(".py"):
            continue
        source = safe_path(root, relative).read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=relative)
        except SyntaxError:
            sites.append(
                {
                    "id": f"{relative}:syntax:0",
                    "file": relative,
                    "function": "",
                    "line": 0,
                    "source_hash": source_hash,
                    "status": "manual",
                    "reasons": ["Python syntax error; cannot exclude classifier calls"],
                }
            )
            continue
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        has_openai = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "openai":
                has_openai = True
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "openai":
                        has_openai = True
        file_sites = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            chain = ast.unparse(node.func)
            # Inventory unresolved wrappers and aliases as manual, not just eligible calls.
            is_request = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"create", "parse"}
                and (has_openai or "responses" in chain or "completions" in chain)
            )
            if not is_request:
                continue
            scope = parents.get(node)
            functions = []
            while scope is not None:
                if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    functions.append(scope)
                scope = parents.get(scope)
            function = functions[0] if functions else None
            qualified = ".".join(part.name for part in reversed(functions)) or "<module>"
            item: dict[str, Any] = {
                "id": f"{relative}:{qualified}:{node.lineno}",
                "file": relative,
                "function": qualified,
                "line": node.lineno,
                "end_line": node.end_lineno,
                "function_line": getattr(function, "lineno", node.lineno),
                "function_end_line": getattr(function, "end_lineno", node.end_lineno),
                "source_hash": source_hash,
                "status": "manual",
                "reasons": [],
            }
            try:
                item.update(_supported(tree, function, node, source))
                item["status"] = "supported"
            except ValueError as error:
                item["reasons"] = [str(error)]
            sites.append(item)
            file_sites += 1
        # Aliased methods, dynamic factories and provider usage without a known method
        # shape cannot be certified safe merely because no create/parse node was found.
        provider_alias = any(
            (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").startswith("openai")
                and any(alias.name != "OpenAI" or alias.asname for alias in node.names)
            )
            or (
                isinstance(node, ast.Import)
                and any(alias.name.startswith("openai") for alias in node.names)
            )
            for node in ast.walk(tree)
        )
        unresolved = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Attribute)
            and ("responses" in ast.unparse(node.value) or "completions" in ast.unparse(node.value))
        ]
        if (has_openai and not file_sites) or unresolved or provider_alias:
            sites.append(
                {
                    "id": f"{relative}:unresolved:0",
                    "file": relative,
                    "function": "",
                    "line": 0,
                    "source_hash": source_hash,
                    "status": "manual",
                    "reasons": ["unresolved OpenAI usage or method alias requires manual review"],
                }
            )
    sites.sort(key=lambda item: item["id"])
    return {
        "schema_version": "sanka-jev-inventory/v1",
        "source_digest": object_digest(files),
        "files": files,
        "call_sites": sites,
        "manual_count": sum(item["status"] == "manual" for item in sites),
    }

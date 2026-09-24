# SPDX-License-Identifier: Apache-2.0
"""Static import identities and ordered conventional Django URL composition."""

from __future__ import annotations

import ast
import copy
import re
from pathlib import Path
from typing import cast


def normalize(tree: ast.Module, module: str, symbols: dict[str, dict[str, str]]) -> ast.Module:
    """Resolve explicit aliases without importing or executing application code."""
    tree = copy.deepcopy(tree)
    names: dict[str, str] = {}
    bound: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            owner = node.module or ""
            if node.level:
                if node.level >= len(module.split(".")):
                    raise ValueError(f"{module}:{node.lineno}: relative import escapes package")
                parts = module.split(".")[: -node.level]
                owner = ".".join([*parts, *([owner] if owner else [])])
            if owner not in symbols:
                raise ValueError(f"{module}:{node.lineno}: unqualified import {owner}")
            for alias in node.names:
                local = alias.asname or alias.name
                if local in bound or alias.name not in symbols[owner]:
                    raise ValueError(f"{module}:{node.lineno}: unknown or shadowed import {local}")
                bound.add(local)
                names[local] = symbols[owner][alias.name]
                alias.name, alias.asname = names[local], None
            node.module, node.level = owner, 0
        elif isinstance(node, ast.ClassDef):
            if node.name in bound:
                raise ValueError(f"{module}:{node.lineno}: shadowed class {node.name}")
            bound.add(node.name)
            names[node.name] = symbols.get(module, {}).get(node.name, node.name)
    # Renaming imports must never change local variables or field declarations.
    for descendant in ast.walk(tree):
        if (
            isinstance(descendant, ast.Name)
            and isinstance(descendant.ctx, ast.Store)
            and descendant.id in names
        ):
            raise ValueError(f"{module}:{descendant.lineno}: shadowed symbol {descendant.id}")
        if isinstance(descendant, ast.arg) and descendant.arg in names:
            raise ValueError(f"{module}:{descendant.lineno}: shadowed argument {descendant.arg}")

    class Rename(ast.NodeTransformer):
        def visit_Name(self, node: ast.Name) -> ast.Name:
            node.id = names.get(node.id, node.id)
            return node

        def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
            node.name = names.get(node.name, node.name)
            self.generic_visit(node)
            return node

    return cast(ast.Module, Rename().visit(tree))


def check_imports(trees: dict[str, ast.Module]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(module: str) -> None:
        if module in visited:
            return
        visiting.add(module)
        for node in trees[module].body:
            if not isinstance(node, ast.ImportFrom):
                continue
            owner = node.module or ""
            if node.level:
                if node.level >= len(module.split(".")):
                    raise ValueError(f"{module}:{node.lineno}: relative import escapes package")
                owner = ".".join([*module.split(".")[: -node.level], owner])
            if owner in visiting:
                raise ValueError(f"{module.replace('.', '/')}.py:{node.lineno}: import cycle")
            if owner in trees:
                walk(owner)
        visiting.remove(module)
        visited.add(module)

    for module in trees:
        walk(module)


def startup(tree: ast.Module) -> ast.Module:
    """Normalize harmless aliases and the standard main wrapper, validating all AST."""
    tree = copy.deepcopy(tree)
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                local = alias.asname or alias.name
                if local in aliases:
                    raise ValueError("manage.py: duplicate startup import")
                aliases[local] = alias.name
                alias.asname = None

    class Rename(ast.NodeTransformer):
        def visit_Name(self, node: ast.Name) -> ast.Name:
            node.id = aliases.get(node.id, node.id)
            return node

    tree = Rename().visit(tree)
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    if functions:
        function = functions[0]
        signature = cast(ast.FunctionDef, ast.parse("def main(): pass").body[0])
        if (
            len(functions) != 1
            or function.name != "main"
            or function.decorator_list
            or (function.returns is not None and ast.unparse(function.returns) != "None")
            or function.type_params
            or ast.dump(function.args) != ast.dump(signature.args)
            or not isinstance(tree.body[-1], ast.If)
            or ast.dump(tree.body[-1])
            != ast.dump(ast.parse('if __name__ == "__main__": main()').body[0])
        ):
            raise ValueError("manage.py: unqualified main wrapper")
        tree.body.remove(function)
        body = list(function.body)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body.pop(0)
        normalized = []
        for node in body:
            if isinstance(node, ast.Try):
                if len(node.body) != 1 or len(node.handlers) != 1 or node.orelse or node.finalbody:
                    raise ValueError("manage.py: unqualified import fallback")
                handler = node.handlers[0]
                if (
                    ast.unparse(node.body[0])
                    != "from django.core.management import execute_from_command_line"
                    or handler.type is None
                    or ast.unparse(handler.type) != "ImportError"
                    or not handler.name
                    or len(handler.body) != 1
                ):
                    raise ValueError("manage.py: unqualified import fallback")
                raised = handler.body[0]
                if (
                    not isinstance(raised, ast.Raise)
                    or not isinstance(raised.exc, ast.Call)
                    or ast.unparse(raised.exc.func) != "ImportError"
                    or raised.exc.keywords
                    or len(raised.exc.args) != 1
                    or not isinstance(raised.exc.args[0], ast.Constant)
                    or not isinstance(raised.exc.args[0].value, str)
                    or raised.cause is None
                    or ast.unparse(raised.cause) != handler.name
                ):
                    raise ValueError("manage.py: executable import fallback")
                normalized.extend(node.body)
            else:
                normalized.append(node)
        tree.body[-1].body = normalized
    return tree


def urls(
    root: Path, module: str, views: dict[str, dict[str, str]]
) -> tuple[list[dict[str, str]], set[str]]:
    from .drf_project import _literal, _module

    consumed: set[str] = set()
    visiting: set[str] = set()
    symbols = views | {
        "django.urls": {"path": "path", "include": "include"},
        "rest_framework.routers": {"DefaultRouter": "DefaultRouter"},
    }

    def walk(name: str, prefix: str) -> list[dict[str, str]]:
        filename, original = _module(root, name)
        if name in visiting:
            raise ValueError(f"{filename}:1: URL include cycle")
        visiting.add(name)
        consumed.add(filename)
        tree = normalize(original, name, symbols)
        routes: dict[str, list[dict[str, str]]] = {}
        output: list[dict[str, str]] = []
        available: set[str] = set()
        assigned: set[str] = set()
        mounted: set[str] = set()
        finished = False
        for node in tree.body:
            try:
                if finished:
                    raise ValueError("execution after urlpatterns")
                if isinstance(node, ast.ImportFrom):
                    if assigned:
                        raise ValueError("imports must precede URL declarations")
                    available.update(a.name for a in node.names)
                    continue
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                ):
                    key = node.targets[0].id
                    if key in assigned or key in available:
                        raise ValueError("reassigned URL symbol")
                    assigned.add(key)
                    if key != "urlpatterns":
                        if (
                            "DefaultRouter" not in available
                            or ast.unparse(node.value) != "DefaultRouter()"
                        ):
                            raise ValueError("unqualified router declaration")
                        routes[key] = []
                        continue
                    if not isinstance(node.value, ast.List):
                        raise ValueError("urlpatterns must be a literal list")
                    finished = True
                    for entry in node.value.elts:
                        if (
                            not {"path", "include"} <= available
                            or not isinstance(entry, ast.Call)
                            or ast.unparse(entry.func) != "path"
                            or entry.keywords
                            or len(entry.args) != 2
                        ):
                            raise ValueError("URL entries require literal path/includes")
                        part = _literal(entry.args[0])
                        if not isinstance(part, str) or not re.fullmatch(
                            r"(?:[a-zA-Z0-9_-]+/)*", part
                        ):
                            raise ValueError("unqualified URL prefix")
                        include = entry.args[1]
                        if (
                            not isinstance(include, ast.Call)
                            or ast.unparse(include.func) != "include"
                            or include.keywords
                            or len(include.args) != 1
                        ):
                            raise ValueError("unqualified URL include")
                        target = include.args[0]
                        if isinstance(target, ast.Constant) and isinstance(target.value, str):
                            output.extend(walk(target.value, prefix + part))
                        elif (
                            isinstance(target, ast.Attribute)
                            and target.attr == "urls"
                            and isinstance(target.value, ast.Name)
                            and target.value.id in routes
                        ):
                            key = target.value.id
                            if key in mounted:
                                raise ValueError("router mounted more than once")
                            mounted.add(key)
                            output.extend(
                                dict(r, path="/" + prefix + part + r["path"] + "/")
                                for r in routes[key]
                            )
                        else:
                            raise ValueError("unresolved URL include")
                    continue
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                    call = node.value
                    if (
                        not isinstance(call.func, ast.Attribute)
                        or call.func.attr != "register"
                        or not isinstance(call.func.value, ast.Name)
                        or call.func.value.id not in routes
                        or len(call.args) != 2
                        or len(call.keywords) != 1
                        or call.keywords[0].arg != "basename"
                        or not isinstance(call.args[1], ast.Name)
                    ):
                        raise ValueError("unqualified router registration")
                    route, basename = _literal(call.args[0]), _literal(call.keywords[0].value)
                    view = call.args[1].id
                    if (
                        view not in available
                        or view not in {v for values in views.values() for v in values.values()}
                        or any(
                            not isinstance(v, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", v)
                            for v in (route, basename)
                        )
                    ):
                        raise ValueError("unresolved router view or prefix")
                    if any(r["basename"] == basename for r in routes[call.func.value.id]):
                        raise ValueError("duplicate basename on the same router")
                    routes[call.func.value.id].append(
                        {"path": route, "basename": basename, "view": view}
                    )
                    continue
                raise ValueError("unqualified URL statement")
            except (ValueError, TypeError) as error:
                raise ValueError(f"{filename}:{node.lineno}: {error}") from error
        visiting.remove(name)
        if not finished or set(routes) != mounted:
            raise ValueError(f"{filename}:1: missing urlpatterns or unmounted router")
        return output

    return walk(module, ""), consumed

# SPDX-License-Identifier: Apache-2.0
"""Lower bounded async sessions to the existing synchronous contract checker."""

from __future__ import annotations

import ast
import copy


def normalize_async_persistence(tree: ast.Module) -> ast.Module:
    imports = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "sqlalchemy.ext.asyncio"
    ]
    if not imports:
        return tree
    tree = copy.deepcopy(tree)
    imports = [node for node in tree.body if isinstance(node, ast.ImportFrom)]
    names = {
        (node.module, alias.name, alias.asname, node.level)
        for node in imports
        for alias in node.names
    }
    expected = {
        ("sqlalchemy.ext.asyncio", "AsyncSession", None, 0),
        ("sqlalchemy.ext.asyncio", "create_async_engine", None, 0),
        ("sqlalchemy.pool", "NullPool", None, 0),
    }
    if {
        item for item in names if item[0] in {"sqlalchemy.ext.asyncio", "sqlalchemy.pool"}
    } != expected:
        raise ValueError(
            "async persistence requires explicit AsyncSession, create_async_engine and NullPool"
        )
    # Added sync symbols must never overwrite a source binding.
    if any(
        (isinstance(node, ast.Name) and node.id in {"Session", "create_engine"})
        or (
            isinstance(node, ast.alias)
            and (node.asname or node.name) in {"Session", "create_engine"}
        )
        for node in ast.walk(tree)
    ):
        raise ValueError("async persistence normalization symbol collision")
    engines = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and ast.unparse(node)
        == "engine = create_async_engine(environ['DATABASE_URL'], poolclass=NullPool)"
    ]
    if len(engines) != 1:
        raise ValueError("async engine requires DATABASE_URL and NullPool")
    engines[0].value = ast.parse("create_engine(environ['DATABASE_URL'])", mode="eval").body
    helpers = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and not node.decorator_list
    }
    used: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef) or not node.decorator_list:
            continue
        if any(
            not isinstance(decorator, ast.Call)
            or not isinstance(decorator.func, ast.Attribute)
            or decorator.func.attr not in {"post", "put", "patch", "delete"}
            for decorator in node.decorator_list
        ):
            raise ValueError("async persistence currently qualifies CRUD writes only")
        tail = node.body[-1]
        if isinstance(tail, ast.Return) and isinstance(tail.value, ast.Await):
            call = tail.value.value
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id in helpers
            ):
                helper = helpers[call.func.id]
                if (
                    helper.returns
                    or helper.type_params
                    or helper.args.defaults
                    or helper.args.kw_defaults
                    or helper.args.kwonlyargs
                    or helper.args.posonlyargs
                    or helper.args.vararg
                    or helper.args.kwarg
                    or any(arg.annotation for arg in helper.args.args)
                    or call.keywords
                    or [ast.unparse(arg) for arg in call.args]
                    != [arg.arg for arg in helper.args.args]
                    or not {arg.arg for arg in helper.args.args}
                    <= {arg.arg for arg in node.args.args}
                    or len(helper.body) != 1
                    or not isinstance(helper.body[0], ast.AsyncWith)
                ):
                    raise ValueError(
                        "async repository requires direct unchanged arguments and one session scope"
                    )
                node.body[-1:] = copy.deepcopy(helper.body)
                used.add(helper.name)
        scopes = [item for item in ast.walk(node) if isinstance(item, ast.AsyncWith)]
        if len(scopes) != 1 or scopes[0] is not node.body[-1]:
            raise ValueError("async handler requires one final session scope")
        scope = scopes[0]
        if (
            len(scope.items) != 1
            or ast.unparse(scope.items[0].context_expr) != "AsyncSession(engine)"
            or scope.items[0].optional_vars is None
            or ast.unparse(scope.items[0].optional_vars) != "session"
        ):
            raise ValueError(
                "async persistence requires async with AsyncSession(engine) as session"
            )
        awaited = []
        for item in ast.walk(node):
            if not isinstance(item, ast.Await):
                continue
            call = item.value
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and ast.unparse(call.func.value) == "session"
                and call.func.attr in {"get", "delete", "commit", "refresh", "execute"}
            ):
                raise ValueError("unsupported awaited persistence operation")
            awaited.append(call)
        for item in ast.walk(node):
            if (
                isinstance(item, ast.Call)
                and isinstance(item.func, ast.Attribute)
                and ast.unparse(item.func.value) == "session"
                and item.func.attr != "add"
                and item not in awaited
            ):
                raise ValueError("async session operations must be awaited")

        class Lower(ast.NodeTransformer):
            def visit_Await(self, item: ast.Await) -> ast.expr:
                return item.value

        node.body = [Lower().visit(item) for item in node.body]
        node.body[-1] = ast.With(items=scope.items, body=scope.body, type_comment=None)
        scope.items[0].context_expr = ast.parse("Session(engine)", mode="eval").body
    if used != helpers.keys():
        raise ValueError("unused or unsupported async repository")
    result = []
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef):
            if node.name in helpers:
                continue
            node = ast.FunctionDef(**{field: getattr(node, field) for field in node._fields})
        if isinstance(node, ast.ImportFrom) and node.module in {
            "sqlalchemy.ext.asyncio",
            "sqlalchemy.pool",
        }:
            continue
        result.append(node)
    tree.body = (
        ast.parse("from sqlalchemy import create_engine\nfrom sqlalchemy.orm import Session").body
        + result
    )
    return ast.fix_missing_locations(tree)

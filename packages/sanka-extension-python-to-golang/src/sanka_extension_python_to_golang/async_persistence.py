# SPDX-License-Identifier: Apache-2.0
"""Lower bounded async sessions to the existing synchronous contract checker."""

from __future__ import annotations

import ast
import copy


def _inline_repository(helper: ast.AsyncFunctionDef, call: ast.Call, first: str) -> list[ast.stmt]:
    args = helper.args
    if (
        helper.decorator_list
        or helper.returns
        or helper.type_params
        or args.defaults
        or args.kw_defaults
        or args.kwonlyargs
        or args.posonlyargs
        or args.vararg
        or args.kwarg
        or call.keywords
        or not args.args
        or args.args[0].arg != first
        or any(
            arg.annotation is not None
            and ast.unparse(arg.annotation) not in {"AsyncSession", "dict", "int", "str", "bool"}
            for arg in args.args
        )
        or [ast.unparse(arg) for arg in call.args]
        != [arg.arg for arg in args.args[(1 if first == "self" else 0) :]]
    ):
        raise ValueError("injected repositories require unchanged positional arguments")
    body = copy.deepcopy(helper.body)
    if first == "self":

        class SessionReceiver(ast.NodeTransformer):
            def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
                if ast.unparse(node) == "self.session":
                    return ast.copy_location(ast.Name(id="session", ctx=node.ctx), node)
                return self.generic_visit(node)

        body = [SessionReceiver().visit(node) for node in body]
    return body


def _injected_sessions(tree: ast.Module) -> set[tuple[str | None, str]]:
    """Expand one side-effect-free yield dependency and its explicit repositories."""
    if not any(
        isinstance(node, ast.ImportFrom)
        and node.module == "fastapi"
        and not node.level
        and any(alias.name == "Depends" and alias.asname is None for alias in node.names)
        for node in tree.body
    ):
        return set()
    functions = {node.name: node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)}
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    lowered: set[tuple[str | None, str]] = set()
    providers: set[str] = set()
    helpers: set[str] = set()
    used_methods: dict[str, set[str]] = {}
    for route in functions.values():
        if not route.decorator_list or not route.args.defaults:
            continue
        args = route.args
        default = args.defaults[-1]
        if not (
            len(args.defaults) == 1
            and args.args
            and ast.unparse(args.args[-1]) == "session: AsyncSession"
            and isinstance(default, ast.Call)
            and ast.unparse(default.func) == "Depends"
            and len(default.args) == 1
            and isinstance(default.args[0], ast.Name)
            and not default.keywords
        ):
            raise ValueError("async dependencies require session: AsyncSession = Depends(provider)")
        name = default.args[0].id
        provider = functions.get(name)
        expected = ast.parse(
            f"async def {name}():\n"
            "    async with AsyncSession(engine) as session:\n"
            "        yield session\n"
        ).body[0]
        if provider is None or ast.dump(provider) != ast.dump(expected):
            raise ValueError("session dependency must only yield one AsyncSession(engine)")
        providers.add(name)
        args.args.pop()
        args.defaults.clear()
        body = route.body
        tail = body[-1]
        if isinstance(tail, ast.Return) and isinstance(tail.value, ast.Await):
            call = tail.value.value
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                helper = functions.get(call.func.id)
                if helper is None:
                    raise ValueError("unknown injected repository")
                body[-1:] = _inline_repository(helper, call, "session")
                lowered.add((None, helper.name))
                helpers.add(helper.name)
            elif isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
                if len(body) < 2:
                    raise ValueError("repository must be constructed in the handler")
                construct = body[-2]
                if not (
                    isinstance(construct, ast.Assign)
                    and len(construct.targets) == 1
                    and ast.unparse(construct.targets[0]) == "repository"
                    and ast.unparse(call.func.value) == "repository"
                    and isinstance(construct.value, ast.Call)
                    and isinstance(construct.value.func, ast.Name)
                    and [ast.unparse(arg) for arg in construct.value.args] == ["session"]
                    and not construct.value.keywords
                ):
                    raise ValueError("repository construction must pass the injected session")
                cls = classes.get(construct.value.func.id)
                constructor = ast.parse(
                    "def __init__(self, session: AsyncSession):\n    self.session = session"
                ).body[0]
                if (
                    cls is None
                    or cls.bases
                    or cls.keywords
                    or cls.decorator_list
                    or cls.type_params
                    or not cls.body
                    or ast.dump(cls.body[0]) != ast.dump(constructor)
                    or any(
                        not isinstance(item, ast.AsyncFunctionDef) or item.name.startswith("__")
                        for item in cls.body[1:]
                    )
                ):
                    raise ValueError(
                        "repository class must only store its session and declare async methods"
                    )
                methods = {
                    item.name: item
                    for item in cls.body[1:]
                    if isinstance(item, ast.AsyncFunctionDef)
                }
                if len(methods) != len(cls.body) - 1 or call.func.attr not in methods:
                    raise ValueError("unknown or duplicate repository method")
                body[-2:] = _inline_repository(methods[call.func.attr], call, "self")
                lowered.add((cls.name, call.func.attr))
                used_methods.setdefault(cls.name, set()).add(call.func.attr)
        # Request validation remains outside the persistence scope, as in the
        # existing write recipe. A yielded session opens no connection itself.
        prefix = []
        if body and ast.unparse(body[0]) == "data = data.model_dump(exclude_unset=True)":
            prefix = body[:1]
            body = body[1:]
        wrapper = ast.parse(
            "async def f():\n    async with AsyncSession(engine) as session:\n        pass"
        ).body[0]
        assert isinstance(wrapper, ast.AsyncFunctionDef)
        scope = wrapper.body[0]
        assert isinstance(scope, ast.AsyncWith)
        scope.body = body
        route.body = [*prefix, scope]
        lowered.add((None, route.name))
    for name, consumed_methods in used_methods.items():
        if consumed_methods != {
            node.name for node in classes[name].body[1:] if isinstance(node, ast.AsyncFunctionDef)
        }:
            raise ValueError("unused repository methods require additional capture")
    discarded = providers | helpers | used_methods.keys()
    tree.body = [
        node
        for node in tree.body
        if not isinstance(node, ast.AsyncFunctionDef | ast.ClassDef) or node.name not in discarded
    ]
    if providers:
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "fastapi":
                node.names = [alias for alias in node.names if alias.name != "Depends"]
        tree.body = [
            node for node in tree.body if not isinstance(node, ast.ImportFrom) or node.names
        ]
    return lowered


def normalize_async_persistence(tree: ast.Module) -> tuple[ast.Module, set[tuple[str | None, str]]]:
    imports = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "sqlalchemy.ext.asyncio"
    ]
    if not imports:
        return tree, set()
    tree = copy.deepcopy(tree)
    declared: set[str] = set()
    for node in tree.body:
        symbols = []
        if isinstance(node, ast.ImportFrom):
            symbols = [alias.asname or alias.name for alias in node.names]
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            symbols = [node.name]
        elif isinstance(node, ast.Assign):
            symbols = [target.id for target in node.targets if isinstance(target, ast.Name)]
        for symbol in symbols:
            if symbol in declared:
                raise ValueError("async persistence symbols must not be reassigned")
            declared.add(symbol)
    lowered = _injected_sessions(tree)
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
    return ast.fix_missing_locations(tree), lowered

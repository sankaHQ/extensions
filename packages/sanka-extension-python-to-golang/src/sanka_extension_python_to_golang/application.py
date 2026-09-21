# SPDX-License-Identifier: Apache-2.0
"""Static application composition; no source code is executed."""

from __future__ import annotations

import ast
import builtins
import copy
from typing import Any


def _bindings(node: ast.stmt) -> list[str]:
    if isinstance(node, ast.ImportFrom):
        return [alias.asname or alias.name for alias in node.names]
    if isinstance(node, ast.Assign):
        return [target.id for target in node.targets if isinstance(target, ast.Name)]
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return [node.name]
    return []


def normalize_application(tree: ast.Module, framework: str) -> tuple[ast.Module, dict[str, Any]]:
    tree = copy.deepcopy(tree)
    evidence: dict[str, Any] = {}
    declarations: set[str] = set()
    for node in tree.body:
        names = _bindings(node)
        if declarations.intersection(names):
            raise ValueError("application symbols must not be reassigned")
        declarations.update(names)
    constants: dict[str, ast.expr] = {}
    result = []
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.isupper()
        ):
            name = node.targets[0].id
            if name in vars(builtins) or any(
                (isinstance(item, ast.arg) and item.arg == name)
                or (
                    isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                    and item.name == name
                )
                or (isinstance(item, ast.alias) and (item.asname or item.name) == name)
                or (
                    isinstance(item, ast.Name)
                    and item.id == name
                    and isinstance(item.ctx, ast.Store)
                    and item is not node.targets[0]
                )
                for item in ast.walk(tree)
            ):
                raise ValueError("configuration bindings must not be shadowed")
            if isinstance(node.value, ast.Constant) and type(node.value.value) in {str, int, bool}:
                constants[name] = node.value
            elif name == "DATABASE_URL" and ast.unparse(node.value) == "environ['DATABASE_URL']":
                consumers = [
                    item
                    for item in ast.walk(tree)
                    if isinstance(item, ast.Name)
                    and item.id == name
                    and isinstance(item.ctx, ast.Load)
                ]
                engines = [
                    item
                    for item in tree.body
                    if isinstance(item, ast.Assign)
                    and len(item.targets) == 1
                    and ast.unparse(item.targets[0]) == "engine"
                    and isinstance(item.value, ast.Call)
                    and item.value.args
                    and item.value.args[0] in consumers
                ]
                if len(consumers) != 1 or len(engines) != 1:
                    raise ValueError(
                        "DATABASE_URL configuration must initialize the database engine"
                    )
                constants[name] = node.value
            else:
                raise ValueError(
                    "configuration requires literal values or DATABASE_URL from environ"
                )
            continue

        class Resolve(ast.NodeTransformer):
            def visit_Name(self, item: ast.Name) -> ast.expr:
                if isinstance(item.ctx, ast.Load) and item.id in constants:
                    return ast.copy_location(copy.deepcopy(constants[item.id]), item)
                return item

        result.append(Resolve().visit(node))
    tree.body = result
    if constants:
        evidence["configuration"] = sorted(constants)
    if framework not in {"fastapi", "flask"}:
        return tree, evidence
    constructor = "FastAPI" if framework == "fastapi" else "Flask"
    registration = "include_router" if framework == "fastapi" else "register_blueprint"
    factories = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "create_app"
    ]
    if factories:
        factory = factories[0]
        if (
            factory.decorator_list
            or factory.returns
            or factory.type_params
            or ast.unparse(factory.args)
            or len(factory.body) < 2
            or ast.unparse(factory.body[-1]) != "return app"
            or not isinstance(factory.body[0], ast.Assign)
            or len(factory.body[0].targets) != 1
            or ast.unparse(factory.body[0].targets[0]) != "app"
            or not isinstance(factory.body[0].value, ast.Call)
            or ast.unparse(factory.body[0].value.func) != constructor
            or any(
                not (
                    isinstance(item, ast.Expr)
                    and isinstance(item.value, ast.Call)
                    and ast.unparse(item.value.func) == f"app.{registration}"
                )
                for item in factory.body[1:-1]
            )
        ):
            raise ValueError("factory must only construct, register routers and return app")
        calls = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign) and ast.unparse(node) == "app = create_app()"
        ]
        if len(calls) != 1 or tree.body.index(factory) > tree.body.index(calls[0]):
            raise ValueError("factory requires one later app = create_app()")
        available = {
            name for node in tree.body[: tree.body.index(calls[0])] for name in _bindings(node)
        } | {"app", "__name__"}
        required_names = {
            node.id
            for statement in factory.body
            for node in ast.walk(statement)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        }
        if required_names - available:
            raise ValueError("factory dependencies must be defined before invocation")
        tree.body[tree.body.index(calls[0]) : tree.body.index(calls[0]) + 1] = factory.body[:-1]
        tree.body.remove(factory)
        evidence["factory"] = "create_app"
    if framework == "fastapi":
        apps = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and ast.unparse(node.targets[0]) == "app"
            and isinstance(node.value, ast.Call)
        ]
        for app in apps:
            assert isinstance(app.value, ast.Call)
            lifespan = [kw for kw in app.value.keywords if kw.arg == "lifespan"]
            if not lifespan:
                continue
            if len(lifespan) != 1 or ast.unparse(lifespan[0].value) != "lifespan":
                raise ValueError("only an explicit lifespan function is qualified")
            expected = ast.parse("""@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    try:
        yield
    finally:
        await engine.dispose()
""").body[0]
            matches = [node for node in tree.body if ast.dump(node) == ast.dump(expected)]
            imports = {
                (node.module, alias.name, alias.asname, node.level)
                for node in tree.body
                if isinstance(node, ast.ImportFrom)
                for alias in node.names
            }
            required = {
                ("contextlib", "asynccontextmanager", None, 0),
                ("sqlalchemy", "text", None, 0),
                ("fastapi", "FastAPI", None, 0),
            }
            if len(matches) != 1 or not required <= imports:
                raise ValueError("lifespan requires a database ping and finally-dispose recipe")
            if tree.body.index(matches[0]) > tree.body.index(app):
                raise ValueError("lifespan must precede application construction")
            definition_imports = {
                (node.module, alias.name, alias.asname, node.level)
                for node in tree.body[: tree.body.index(matches[0])]
                if isinstance(node, ast.ImportFrom)
                for alias in node.names
            }
            if (
                not {
                    ("fastapi", "FastAPI", None, 0),
                    ("contextlib", "asynccontextmanager", None, 0),
                }
                <= definition_imports
            ):
                raise ValueError(
                    "lifespan definition requires its annotation and decorator imports"
                )
            tree.body.remove(matches[0])
            app.value.keywords.remove(lifespan[0])
            for node in tree.body:
                if isinstance(node, ast.ImportFrom):
                    node.names = [
                        a
                        for a in node.names
                        if (node.module, a.name, a.asname, node.level)
                        not in required - {("fastapi", "FastAPI", None, 0)}
                    ]
            tree.body = [
                node for node in tree.body if not isinstance(node, ast.ImportFrom) or node.names
            ]
            evidence["lifespan"] = {"startup": "database-ping", "shutdown": "database-dispose"}
    return ast.fix_missing_locations(tree), evidence

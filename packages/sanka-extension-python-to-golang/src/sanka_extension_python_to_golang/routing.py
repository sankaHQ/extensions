# SPDX-License-Identifier: Apache-2.0
"""Normalize qualified routing declarations without executing source code."""

from __future__ import annotations

import ast
import copy
import re
import sys
from pathlib import Path

RESERVED_MODULES = sys.stdlib_module_names | {
    "flask",
    "fastapi",
    "django",
    "rest_framework",
    "sqlalchemy",
    "pydantic",
    "migration_source",
}


def _prefix(node: ast.expr) -> str:
    value = ast.literal_eval(node)
    if type(value) is not str or not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)*", value):
        raise ValueError("router prefixes require literal static path segments")
    return value


def normalize_routes(tree: ast.Module, framework: str) -> ast.Module:
    tree = copy.deepcopy(tree)
    if framework == "drf":
        return _django(tree)
    constructor = "Blueprint" if framework == "flask" else "APIRouter"
    registration = "register_blueprint" if framework == "flask" else "include_router"
    prefix_key = "url_prefix" if framework == "flask" else "prefix"
    imported = any(
        isinstance(node, ast.ImportFrom)
        and node.module == framework
        and node.level == 0
        and any(alias.name == constructor and alias.asname is None for alias in node.names)
        for node in tree.body
    )
    if not imported:
        return tree
    # Consumed declarations must not hide rebinding or counterfeit framework imports.
    names: set[str] = set()
    for node in tree.body:
        declared = []
        if isinstance(node, ast.ImportFrom):
            declared = [alias.asname or alias.name for alias in node.names]
        elif isinstance(node, ast.Assign):
            declared = [target.id for target in node.targets if isinstance(target, ast.Name)]
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            declared = [node.name]
        for name in declared:
            if name in names:
                raise ValueError("routing symbols must not be reassigned")
            names.add(name)
    blueprint_names: set[str] = set()
    imported_constructor = False
    app_seen = False
    routers: dict[str, str] = {}
    registered: dict[str, str] = {}
    definitions: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    body: list[ast.stmt] = []
    factory: ast.FunctionDef | None = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "create_app":
            if (
                framework != "flask"
                or node.decorator_list
                or node.returns
                or node.type_params
                or ast.unparse(node.args)
            ):
                raise ValueError("only a zero-argument Flask factory is qualified")
            if (
                len(node.body) < 3
                or ast.unparse(node.body[0]) != "app = Flask(__name__)"
                or ast.unparse(node.body[-1]) != "return app"
            ):
                raise ValueError(
                    "factory must only construct app, register blueprints and return app"
                )
            if any(not _registration(item, registration) for item in node.body[1:-1]):
                raise ValueError("factory configuration and hooks require additional capture")
            factory = node
            continue
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            name, value = node.targets[0].id, node.value
            if name == "app" and ast.unparse(value) == "create_app()" and factory is not None:
                body.extend(factory.body[:-1])
                factory = None
                continue
        body.append(node)
    if factory is not None:
        raise ValueError("factory must be instantiated with app = create_app()")
    result: list[ast.stmt] = []
    for node in body:
        if isinstance(node, ast.ImportFrom) and node.module == framework:
            if any(alias.name == constructor and alias.asname for alias in node.names):
                raise ValueError("aliased router constructors require additional capture")
            imported_constructor |= any(alias.name == constructor for alias in node.names)
            node.names = [alias for alias in node.names if alias.name != constructor]
            if node.names:
                result.append(node)
            continue
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            name, value = node.targets[0].id, node.value
            if name == "app":
                app_seen = True
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == constructor
            ):
                if (
                    not imported_constructor
                    or name in {"app", "engine"}
                    or any(k.arg != prefix_key for k in value.keywords)
                    or len(value.keywords) > 1
                ):
                    raise ValueError("custom router options require additional capture")
                if framework == "flask":
                    if (
                        len(value.args) != 2
                        or ast.unparse(value.args[1]) != "__name__"
                        or not isinstance(value.args[0], ast.Constant)
                        or type(value.args[0].value) is not str
                        or not value.args[0].value
                        or "." in value.args[0].value
                    ):
                        raise ValueError("Blueprint requires a literal name and __name__")
                    if value.args[0].value in blueprint_names:
                        raise ValueError("Blueprint names must be unique")
                    blueprint_names.add(value.args[0].value)
                elif value.args:
                    raise ValueError("APIRouter requires keyword arguments")
                routers[name] = _prefix(value.keywords[0].value) if value.keywords else ""
                definitions[name] = []
                continue
        if _registration(node, registration):
            assert isinstance(node, ast.Expr)
            call = node.value
            assert isinstance(call, ast.Call)
            assert isinstance(call.func, ast.Attribute)
            assert isinstance(call.func.value, ast.Name)
            if (
                not app_seen
                or call.func.value.id != "app"
                or len(call.args) != 1
                or not isinstance(call.args[0], ast.Name)
                or any(k.arg != prefix_key for k in call.keywords)
                or len(call.keywords) > 1
            ):
                raise ValueError("only app registration with a literal prefix is qualified")
            name = call.args[0].id
            if name not in routers or name in registered or not definitions[name]:
                raise ValueError("routers must be declared with routes and registered exactly once")
            prefix = _prefix(call.keywords[0].value) if call.keywords else ""
            registered[name] = (
                (prefix if call.keywords else routers[name])
                if framework == "flask"
                else prefix + routers[name]
            )
            continue
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for decorator in node.decorator_list:
                if not app_seen and any(
                    isinstance(n, ast.Name) and n.id == "app" for n in ast.walk(decorator)
                ):
                    raise ValueError("app routes must follow app construction")
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name)
                    and decorator.func.value.id in routers
                ):
                    name = decorator.func.value.id
                    if name in registered:
                        raise ValueError(
                            "routes added after router registration require additional capture"
                        )
                    definitions[name].append(node)
        result.append(node)
    if set(routers) != set(registered):
        raise ValueError("every captured router must be registered")
    for name, functions in definitions.items():
        for function in functions:
            for decorator in function.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name)
                    and decorator.func.value.id == name
                ):
                    if len(decorator.args) != 1:
                        raise ValueError("router routes require one literal path")
                    path = ast.literal_eval(decorator.args[0])
                    if type(path) is not str or not path.startswith("/"):
                        raise ValueError("router route paths must start with /")
                    decorator.func.value.id = "app"
                    decorator.args[0] = ast.Constant(registered[name] + path)
    # App construction precedes decorators in the normalized validation tree only.
    # Replay executes the original source, including its original factory ordering.
    apps = [
        n
        for n in result
        if isinstance(n, ast.Assign)
        and len(n.targets) == 1
        and isinstance(n.targets[0], ast.Name)
        and n.targets[0].id == "app"
    ]
    if len(apps) != 1:
        raise ValueError("routing requires exactly one app")
    result.remove(apps[0])
    index = max((i + 1 for i, n in enumerate(result) if isinstance(n, ast.ImportFrom)), default=0)
    result.insert(index, apps[0])
    tree.body = result
    return ast.fix_missing_locations(tree)


def _registration(node: ast.stmt, method: str) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.attr == method
    )


def _django(tree: ast.Module) -> ast.Module:
    imported = any(
        isinstance(n, ast.ImportFrom)
        and n.module == "django.urls"
        and not n.level
        and any(a.name == "include" and not a.asname for a in n.names)
        for n in tree.body
    )
    if not imported:
        return tree
    if any(
        isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id in {"include", "path"}
        for n in ast.walk(tree)
    ) or any(
        isinstance(n, ast.FunctionDef | ast.ClassDef) and n.name in {"include", "path"}
        for n in ast.walk(tree)
    ):
        raise ValueError("URL helpers must not be reassigned")

    def flatten(items: ast.expr, prefix: str = "") -> list[ast.expr]:
        if not isinstance(items, ast.List):
            raise ValueError("include requires a literal URL list")
        output = []
        for item in items.elts:
            if (
                not isinstance(item, ast.Call)
                or not isinstance(item.func, ast.Name)
                or item.func.id != "path"
                or len(item.args) != 2
                or item.keywords
            ):
                raise ValueError("only path(pattern, view) URL lists are qualified")
            path = ast.literal_eval(item.args[0])
            if type(path) is not str:
                raise ValueError("URL paths must be literal strings")
            view = item.args[1]
            if (
                isinstance(view, ast.Call)
                and isinstance(view.func, ast.Name)
                and view.func.id == "include"
            ):
                if (
                    len(view.args) != 1
                    or view.keywords
                    or not re.fullmatch(r"[A-Za-z0-9_/-]*", path)
                ):
                    raise ValueError("include requires a static prefix and one literal list")
                output.extend(flatten(view.args[0], prefix + path))
            else:
                item.args[0] = ast.Constant(prefix + path)
                output.append(item)
        return output

    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "urlpatterns"
        ):
            node.value = ast.List(elts=flatten(node.value), ctx=ast.Load())
    return ast.fix_missing_locations(tree)


def project_tree(root: Path, filename: str, models_file: str) -> tuple[ast.Module, list[str]]:
    """Inline explicit imports from flat local modules; retain original files for replay."""
    if Path(filename).stem in RESERVED_MODULES:
        raise ValueError("source filename conflicts with runtime imports")
    visited: set[str] = set()
    active: set[str] = set()
    total_bytes = 0
    shared_imports: set[tuple[str | None, str, str | None, int]] = set()

    def read(name: str) -> list[ast.stmt]:
        nonlocal total_bytes
        if name in active:
            raise ValueError("cyclic source imports require additional capture")
        if name in visited:
            raise ValueError("repeated local imports require additional capture")
        if len(visited) >= 32:
            raise ValueError("source module graph exceeds 32 files")
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("source modules must be regular files")
        total_bytes += path.stat().st_size
        if total_bytes > 10_000_000:
            raise ValueError("source module graph exceeds capture size limit")
        active.add(name)
        visited.add(name)
        tree = ast.parse(path.read_text(), filename=name)
        declared: set[str] = set()
        for node in tree.body:
            symbols = []
            if isinstance(node, ast.ImportFrom):
                symbols = [a.asname or a.name for a in node.names]
            elif isinstance(node, ast.Assign):
                symbols = [n.id for n in node.targets if isinstance(n, ast.Name)]
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                symbols = [node.name]
            for symbol in symbols:
                if symbol in declared:
                    raise ValueError("source module symbols must not be reassigned")
                declared.add(symbol)
        # Inlining must not resolve a missing module global through another file.
        for node in tree.body:
            locals_ = {
                n.id
                for n in ast.walk(node)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
            }
            locals_ |= {n.arg for n in ast.walk(node) if isinstance(n, ast.arg)}
            missing = (
                {
                    n.id
                    for n in ast.walk(node)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                }
                - declared
                - locals_
                - {"__name__", "list", "dict", "set", "type", "int", "str", "bool"}
            )
            if missing:
                raise ValueError(f"{name}: unresolved module symbols: {', '.join(sorted(missing))}")
        result = []
        for node in tree.body:
            if (
                isinstance(node, ast.ImportFrom)
                and not node.level
                and node.module
                and node.module.isidentifier()
                and (root / (node.module + ".py")).exists()
                and node.module + ".py" != models_file
            ):
                if node.module in RESERVED_MODULES or any(
                    a.asname or a.name == "*" for a in node.names
                ):
                    raise ValueError("local imports require unaliased, nonconflicting module names")
                statements = read(node.module + ".py")
                exports = {
                    n.name
                    for n in statements
                    if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
                } | {
                    t.id
                    for n in statements
                    if isinstance(n, ast.Assign)
                    for t in n.targets
                    if isinstance(t, ast.Name)
                }
                if not {a.name for a in node.names} <= exports:
                    raise ValueError("local import does not refer to a declared symbol")
                result.extend(statements)
            elif isinstance(node, ast.ImportFrom):
                aliases = []
                for alias in node.names:
                    key = (node.module, alias.name, alias.asname, node.level)
                    if key not in shared_imports:
                        aliases.append(alias)
                        shared_imports.add(key)
                if aliases:
                    node.names = aliases
                    result.append(node)
            else:
                result.append(node)
        active.remove(name)
        return result

    body = read(filename)
    return ast.Module(body=body, type_ignores=[]), sorted(visited - {filename})

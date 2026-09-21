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
        return _drf_methods(_django(tree))
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
    blueprint_names: set[str] = set()
    imported_constructor = False
    app_seen = False
    routers: dict[str, str] = {}
    registered: dict[str, str] = {}
    parents: dict[str, str] = {}
    definitions: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    result: list[ast.stmt] = []
    for node in tree.body:
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
                (call.func.value.id == "app" and not app_seen)
                or call.func.value.id not in {"app", *routers}
                or call.func.value.id in registered
                or len(call.args) != 1
                or not isinstance(call.args[0], ast.Name)
                or any(k.arg != prefix_key for k in call.keywords)
                or len(call.keywords) > 1
            ):
                raise ValueError("only app registration with a literal prefix is qualified")
            name = call.args[0].id
            if (
                name not in routers
                or name in registered
                or name == call.func.value.id
                or not (definitions[name] or name in parents.values())
            ):
                raise ValueError("routers must be declared with routes and registered exactly once")
            parents[name] = call.func.value.id
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
    for name in registered:
        parent = parents[name]
        seen = {name}
        prefix = registered[name]
        while parent != "app":
            if parent in seen:
                raise ValueError("router registration cycle")
            seen.add(parent)
            prefix = registered[parent] + prefix
            parent = parents[parent]
        # Keep the original relative prefixes until every chain has been resolved.
        routers[name] = prefix
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
                    decorator.args[0] = ast.Constant(routers[name] + path)
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


def _drf_methods(tree: ast.Module) -> ast.Module:
    """Django resolves URLs before DRF methods; split explicit dispatch, never URL shadows."""
    names = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }
    names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    replacements: dict[str, list[str]] = {}
    body: list[ast.stmt] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.decorator_list:
            body.append(node)
            continue
        decorator = node.decorator_list[0]
        if not (
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Name)
            and decorator.func.id == "api_view"
            and len(decorator.args) == 1
            and not decorator.keywords
        ):
            body.append(node)
            continue
        methods = ast.literal_eval(decorator.args[0])
        if not isinstance(methods, list) or len(methods) < 2:
            body.append(node)
            continue
        if any(
            type(method) is not str or method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}
            for method in methods
        ) or len(set(methods)) != len(methods):
            raise ValueError("DRF method lists require unique qualified methods")
        branches: dict[str, list[ast.stmt]] = {}
        pending = list(node.body)
        while pending:
            branch = pending.pop(0)
            if not isinstance(branch, ast.If):
                raise ValueError("multi-method views require explicit request.method branches")
            test = branch.test
            if not (
                isinstance(test, ast.Compare)
                and ast.unparse(test.left) == "request.method"
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq)
                and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and type(test.comparators[0].value) is str
            ):
                raise ValueError("multi-method views require literal method equality checks")
            method = test.comparators[0].value
            if method not in methods or method in branches:
                raise ValueError("DRF method branches must match the declared methods once")
            branches[method] = branch.body
            pending = branch.orelse + pending
        if set(branches) != set(methods):
            raise ValueError("every declared DRF method requires a branch")
        replacements[node.name] = []
        for method in methods:
            name = node.name + "__sanka_" + method.lower()
            if name in names:
                raise ValueError("multi-method view normalization name collision")
            names.add(name)
            function = copy.deepcopy(node)
            function.name = name
            function.body = branches[method]
            function.decorator_list[0] = ast.parse(f"api_view([{method!r}])", mode="eval").body
            body.append(function)
            replacements[node.name].append(name)
    for node in body:
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "urlpatterns"
            and isinstance(node.value, ast.List)
        ):
            continue
        paths: list[str] = []
        expanded = []
        for route in node.value.elts:
            if not (
                isinstance(route, ast.Call)
                and len(route.args) == 2
                and isinstance(route.args[1], ast.Name)
            ):
                expanded.append(route)
                continue
            path = ast.literal_eval(route.args[0])
            if not isinstance(path, str):
                raise ValueError("URL paths must be literal strings")

            def pattern(value: str) -> str:
                return re.sub(r"<int:[a-z][a-z0-9_]*>", "[0-9]+", re.escape(value))

            if any(
                pattern(path) == pattern(previous)
                or re.fullmatch(pattern(path), previous)
                or re.fullmatch(pattern(previous), path)
                for previous in paths
            ):
                raise ValueError("overlapping DRF URLs require one method-dispatch view")
            paths.append(path)
            for name in replacements.get(route.args[1].id, [route.args[1].id]):
                cloned = copy.deepcopy(route)
                cloned.args[1] = ast.Name(id=name, ctx=ast.Load())
                expanded.append(cloned)
        node.value.elts = expanded
    tree.body = body
    return ast.fix_missing_locations(tree)


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
    """Inline an explicit package graph, retaining original files for replay."""
    if Path(filename).stem in RESERVED_MODULES:
        raise ValueError("source filename conflicts with runtime imports")
    visited: set[str] = set()
    active: set[str] = set()
    total_bytes = 0
    module_exports: dict[str, set[str]] = {}
    exports: dict[str, set[str]] = {}
    shared_imports: set[tuple[str | None, str, str | None, int]] = set()

    def packages(name: str) -> None:
        nonlocal total_bytes
        for parent in reversed(Path(name).parents):
            if parent == Path("."):
                continue
            if (root / parent).is_symlink():
                raise ValueError("source packages must not be symlinks")
            init = (parent / "__init__.py").as_posix()
            path = root / init
            if not path.is_file() or path.is_symlink():
                raise ValueError("source packages require regular __init__.py files")
            if init not in visited:
                total_bytes += path.stat().st_size
                if len(visited) >= 32 or total_bytes > 10_000_000:
                    raise ValueError("source package graph exceeds capture limits")
                body = ast.parse(path.read_text()).body
                if any(
                    not isinstance(item, ast.Pass)
                    and not (
                        isinstance(item, ast.Expr)
                        and isinstance(item.value, ast.Constant)
                        and type(item.value.value) is str
                    )
                    for item in body
                ):
                    raise ValueError("package initializer behavior requires additional capture")
                visited.add(init)

    def local_module(node: ast.ImportFrom, name: str) -> str | None:
        if node.level:
            parents = list(Path(name).parent.parts)
            if node.level > len(parents) or not node.module:
                raise ValueError("relative imports must name a module inside the source package")
            parts = parents[: len(parents) - node.level + 1] + node.module.split(".")
        elif node.module:
            parts = node.module.split(".")
        else:
            return None
        if not all(part.isidentifier() for part in parts):
            raise ValueError("source imports require canonical module names")
        path = Path(*parts).with_suffix(".py")
        candidate = root / path
        if (root / Path(*parts) / "__init__.py").exists():
            raise ValueError("import a declared symbol from its module, not a package initializer")
        if candidate.exists():
            if parts[0] in RESERVED_MODULES:
                raise ValueError("source package conflicts with runtime imports")
            packages(path.as_posix())
            return path.as_posix()
        if node.level:
            raise ValueError("relative source module is missing")
        return None

    def read(name: str) -> list[ast.stmt]:
        nonlocal total_bytes
        if name in active:
            raise ValueError("cyclic source imports require additional capture")
        if name in visited:
            return []
        if name != filename or any(
            (root / parent / "__init__.py").exists()
            for parent in Path(name).parents
            if parent != Path(".")
        ):
            packages(name)
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
            immediate = (
                [node.value]
                if isinstance(node, ast.Assign)
                else (
                    node.decorator_list
                    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                    else []
                )
            )
            immediate_names = {
                n.id
                for expression in immediate
                for n in ast.walk(expression)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            }
            if immediate_names - declared - {"__name__"}:
                raise ValueError(f"{name}: declaration uses a symbol before it is defined")
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
        module_exports[name] = declared
        exports[name] = {
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        } | {
            target.id
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
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
                - {"__name__", "list", "dict", "set", "type", "int", "str", "bool", "len"}
            )
            if missing:
                raise ValueError(f"{name}: unresolved module symbols: {', '.join(sorted(missing))}")
        result = []
        for node in tree.body:
            local = local_module(node, name) if isinstance(node, ast.ImportFrom) else None
            if isinstance(node, ast.ImportFrom) and local == models_file:
                node.module = Path(models_file).stem
                node.level = 0
                local = None
            if isinstance(node, ast.ImportFrom) and local:
                if any(a.asname or a.name == "*" for a in node.names):
                    raise ValueError("local imports require unaliased, nonconflicting module names")
                statements = read(local)
                imported = {a.name for a in node.names}
                if not imported <= exports[local] & module_exports[local]:
                    raise ValueError("local import does not refer to a declared symbol")
                exports[name].update(imported)
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

# SPDX-License-Identifier: Apache-2.0
"""Static FastAPI application topology. Source code is never imported."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any


def _module_file(root: Path, module: str) -> Path | None:
    path = root.joinpath(*module.split("."))
    for candidate in (path.with_suffix(".py"), path / "__init__.py"):
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    return None


def _imports(root: Path, tree: ast.Module, source: Path) -> dict[str, tuple[Path, str]]:
    result: dict[str, tuple[Path, str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        parts = list(source.relative_to(root).parts[:-1])
        if node.level:
            if node.level > len(parts):
                continue
            module = ".".join(parts[: len(parts) - node.level + 1] + node.module.split("."))
        else:
            module = node.module
        path = _module_file(root, module)
        if path is None:
            continue
        for alias in node.names:
            if alias.name != "*":
                result[alias.asname or alias.name] = (path, alias.name)
    return result


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_call_name(node.value)}.{node.attr}"
    return ast.unparse(node)


def _literal_string(
    node: ast.expr | None, tree: ast.Module, imports: dict[str, tuple[Path, str]]
) -> str | None:
    if isinstance(node, ast.Name):
        name = node.id
        if name in imports:
            path, name = imports[name]
            tree = ast.parse(path.read_text())
        values = [
            item.value
            for item in tree.body
            if isinstance(item, ast.Assign)
            and len(item.targets) == 1
            and isinstance(item.targets[0], ast.Name)
            and item.targets[0].id == name
        ]
        node = values[0] if len(values) == 1 else None
    return node.value if isinstance(node, ast.Constant) and type(node.value) is str else None


def _keywords(
    call: ast.Call, *, exclude: set[str] | frozenset[str] = frozenset()
) -> dict[str, str]:
    return {
        keyword.arg: ast.unparse(keyword.value)
        for keyword in call.keywords
        if keyword.arg and keyword.arg not in exclude
    }


def _ordered_calls(node: ast.AST) -> list[ast.Call]:
    return sorted(
        (item for item in ast.walk(node) if isinstance(item, ast.Call)),
        key=lambda item: (item.lineno, item.col_offset),
    )


def _dependency_call(call: ast.Call) -> dict[str, Any] | None:
    if (
        not isinstance(call.func, ast.Name)
        or call.func.id not in {"Depends", "Security"}
        or len(call.args) != 1
    ):
        return None
    result: dict[str, Any] = {"kind": call.func.id, "call": _call_name(call.args[0])}
    scopes = next((keyword.value for keyword in call.keywords if keyword.arg == "scopes"), None)
    if scopes is not None:
        try:
            values = ast.literal_eval(scopes)
        except (TypeError, ValueError):
            return None
        if not isinstance(values, list) or not all(type(value) is str for value in values):
            return None
        result["scopes"] = values
    return result


def _dependency_list(node: ast.expr | None) -> list[dict[str, Any]] | None:
    if node is None:
        return []
    if not isinstance(node, ast.List | ast.Tuple):
        return None
    result = []
    for item in node.elts:
        dependency = _dependency_call(item) if isinstance(item, ast.Call) else None
        if dependency is None:
            return None
        result.append(dependency)
    return result


def _dependencies(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    arguments = [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
    for argument in arguments:
        if argument.annotation is None:
            continue
        calls = [
            node
            for node in ast.walk(argument.annotation)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"Depends", "Security"}
        ]
        if not calls:
            continue
        call = calls[0]
        dependency = _dependency_call(call)
        if dependency is None:
            continue
        dependency = {"parameter": argument.arg, **dependency}
        result.append(dependency)
    return result


def _exception_handlers(
    root: Path, imports: dict[str, tuple[Path, str]], call: ast.Call
) -> list[dict[str, Any]]:
    if not isinstance(call.func, ast.Name) or call.func.id not in imports:
        return []
    path, function_name = imports[call.func.id]
    tree = ast.parse(path.read_text(), filename=path.relative_to(root).as_posix())
    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == function_name
        ),
        None,
    )
    if function is None:
        return []
    result: list[dict[str, Any]] = []
    for handler_call in _ordered_calls(function):
        if (
            isinstance(handler_call.func, ast.Attribute)
            and handler_call.func.attr == "add_exception_handler"
            and len(handler_call.args) == 2
        ):
            result.append(
                {
                    "exception": _call_name(handler_call.args[0]),
                    "handler": _call_name(handler_call.args[1]),
                    "order": len(result),
                }
            )
    return result


def _router(
    root: Path,
    path: Path,
    name: str,
    inherited_prefix: str,
    include_dependencies: list[dict[str, Any]],
    queue: list[tuple[Path, str, str, list[dict[str, Any]]]],
    gaps: list[str],
) -> dict[str, Any] | None:
    relative = path.relative_to(root).as_posix()
    tree = ast.parse(path.read_text(), filename=relative)
    imports = _imports(root, tree, path)
    assignment = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
            and isinstance(node.value, ast.Call)
            and _call_name(node.value.func) == "APIRouter"
        ),
        None,
    )
    if assignment is None:
        gaps.append(f"{relative}:{name}: imported router is not a static APIRouter")
        return None
    assert isinstance(assignment.value, ast.Call)
    prefix_node = next(
        (keyword.value for keyword in assignment.value.keywords if keyword.arg == "prefix"), None
    )
    router_prefix = _literal_string(prefix_node, tree, imports)
    if prefix_node is not None and router_prefix is None:
        gaps.append(f"{relative}:{name}: dynamic router prefix")
        router_prefix = ""
    prefix = inherited_prefix + (router_prefix or "")
    dependency_node = next(
        (keyword.value for keyword in assignment.value.keywords if keyword.arg == "dependencies"),
        None,
    )
    dependencies = _dependency_list(dependency_node)
    if dependencies is None:
        gaps.append(f"{relative}:{name}: dynamic router dependencies")
        dependencies = []
    routes = []
    for function in tree.body:
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in function.decorator_list:
            if not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == name
                and decorator.func.attr.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
                and decorator.args
            ):
                continue
            route_path = _literal_string(decorator.args[0], tree, imports)
            if route_path is None:
                gaps.append(f"{relative}:{function.name}: dynamic route path")
                continue
            decorator_dependency_node = next(
                (keyword.value for keyword in decorator.keywords if keyword.arg == "dependencies"),
                None,
            )
            decorator_dependencies = _dependency_list(decorator_dependency_node)
            if decorator_dependencies is None:
                gaps.append(f"{relative}:{function.name}: dynamic decorator dependencies")
                decorator_dependencies = []
            routes.append(
                {
                    "method": decorator.func.attr.upper(),
                    "path": prefix + route_path,
                    "handler": function.name,
                    "async": isinstance(function, ast.AsyncFunctionDef),
                    "decorator_dependencies": decorator_dependencies,
                    "parameter_dependencies": _dependencies(function),
                }
            )
    for call in _ordered_calls(tree):
        if not (
            isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == name
            and call.func.attr == "include_router"
        ):
            continue
        if len(call.args) != 1 or not isinstance(call.args[0], ast.Name):
            gaps.append(f"{relative}:{name}: dynamic included router")
            continue
        imported = imports.get(call.args[0].id, (path, call.args[0].id))
        child_prefix = next(
            (keyword.value for keyword in call.keywords if keyword.arg == "prefix"), None
        )
        literal = _literal_string(child_prefix, tree, imports)
        if imported is None or (child_prefix is not None and literal is None):
            gaps.append(f"{relative}:{name}: dynamic included router")
            continue
        child_dependency_node = next(
            (keyword.value for keyword in call.keywords if keyword.arg == "dependencies"), None
        )
        child_dependencies = _dependency_list(child_dependency_node)
        if child_dependencies is None:
            gaps.append(f"{relative}:{name}: dynamic include dependencies")
            child_dependencies = []
        queue.append((imported[0], imported[1], prefix + (literal or ""), child_dependencies))
    return {
        "module": relative,
        "name": name,
        "prefix": prefix,
        "options": _keywords(assignment.value, exclude={"prefix", "dependencies"}),
        "dependencies": dependencies,
        "include_dependencies": include_dependencies,
        "routes": routes,
    }


def capture_fastapi_topology(root: Path, source_file: str) -> dict[str, Any]:
    """Capture application composition and dependency order without importing source."""
    entrypoint = root / source_file
    tree = ast.parse(entrypoint.read_text(), filename=source_file)
    imports = _imports(root, tree, entrypoint)
    application_file = source_file
    local_app = any(
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "app" for target in node.targets)
        for node in tree.body
    )
    imported_app = imports.get("app")
    if not local_app and imported_app is not None and imported_app[1] == "app":
        entrypoint = imported_app[0]
        application_file = entrypoint.relative_to(root).as_posix()
        tree = ast.parse(entrypoint.read_text(), filename=application_file)
        imports = _imports(root, tree, entrypoint)
    factory_name = None
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "app"
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
        ):
            factory_name = node.value.func.id
    factory = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == factory_name
        ),
        None,
    )
    scope: ast.AST = factory or tree
    calls = _ordered_calls(scope)
    constructor = next((call for call in calls if _call_name(call.func) == "FastAPI"), None)
    gaps: list[str] = []
    if constructor is None:
        gaps.append(f"{application_file}: FastAPI application constructor not found")
    lifespan = None
    if constructor is not None:
        value = next((item.value for item in constructor.keywords if item.arg == "lifespan"), None)
        lifespan = ast.unparse(value) if value is not None else None
    application_dependency_node = (
        next((item.value for item in constructor.keywords if item.arg == "dependencies"), None)
        if constructor is not None
        else None
    )
    application_dependencies = _dependency_list(application_dependency_node)
    if application_dependencies is None:
        gaps.append(f"{application_file}: dynamic application dependencies")
        application_dependencies = []
    middleware: list[dict[str, Any]] = []
    exception_handlers: list[dict[str, Any]] = []
    queue: list[tuple[Path, str, str, list[dict[str, Any]]]] = []
    for call in calls:
        if (
            isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "app"
            and call.func.attr == "add_middleware"
            and call.args
        ):
            middleware.append(
                {
                    "name": _call_name(call.args[0]),
                    "options": _keywords(call),
                    "registration_order": len(middleware),
                }
            )
        elif (
            isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "app"
            and call.func.attr == "include_router"
        ):
            if len(call.args) != 1 or not isinstance(call.args[0], ast.Name):
                gaps.append(f"{application_file}: dynamic included router")
                continue
            imported = imports.get(call.args[0].id, (entrypoint, call.args[0].id))
            prefix_node = next(
                (keyword.value for keyword in call.keywords if keyword.arg == "prefix"), None
            )
            prefix = _literal_string(prefix_node, tree, imports)
            if imported is None or (prefix_node is not None and prefix is None):
                gaps.append(f"{application_file}: dynamic included router")
            else:
                include_dependency_node = next(
                    (keyword.value for keyword in call.keywords if keyword.arg == "dependencies"),
                    None,
                )
                include_dependencies = _dependency_list(include_dependency_node)
                if include_dependencies is None:
                    gaps.append(f"{application_file}: dynamic include dependencies")
                    include_dependencies = []
                queue.append((imported[0], imported[1], prefix or "", include_dependencies))
        elif call.args and isinstance(call.args[0], ast.Name) and call.args[0].id == "app":
            exception_handlers.extend(_exception_handlers(root, imports, call))
    routers = []
    visited: set[tuple[str, str, str]] = set()
    while queue:
        if len(visited) >= 64:
            gaps.append("router graph exceeds static capture limit")
            break
        path, name, prefix, include_dependencies = queue.pop(0)
        key = (path.relative_to(root).as_posix(), name, prefix)
        if key in visited:
            continue
        visited.add(key)
        captured = _router(root, path, name, prefix, include_dependencies, queue, gaps)
        if captured is not None:
            routers.append(captured)
    return {
        "entrypoint": source_file,
        "factory": factory_name,
        "lifespan": lifespan,
        "dependencies": application_dependencies,
        "middleware": middleware,
        "exception_handlers": exception_handlers,
        "routers": routers,
        "gaps": sorted(set(gaps)),
    }

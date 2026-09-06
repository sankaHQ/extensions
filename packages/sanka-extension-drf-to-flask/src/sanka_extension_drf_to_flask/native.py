# SPDX-License-Identifier: Apache-2.0
"""Recognize small DRF components; unsupported custom behavior stays a gap."""

from __future__ import annotations

import ast
import inspect
import sys
import textwrap
from pathlib import Path
from typing import Any, cast


def isolated_module(module: Any, seen: set[str] | None = None) -> bool:
    """Follow local imports before reusing domain code in the native serving process."""
    import importlib

    if module is None or not getattr(module, "__file__", None):
        return False
    seen = set() if seen is None else seen
    if module.__name__ in seen:
        return True
    seen.add(module.__name__)
    allowed = {
        "datetime",
        "decimal",
        "uuid",
        "types",
        "re",
        "math",
        "zoneinfo",
        "django.db",
        "django.db.models",
        "django.utils.http",
    }
    try:
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in {"__import__", "exec", "eval", "open"}:
                return False
            names = []
            if isinstance(node, ast.Import):
                names = [n.name for n in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level or not node.module or any(n.name == "*" for n in node.names):
                    return False
                names = [node.module]
            for name in names:
                if name in allowed:
                    continue
                if name.split(".")[0] in {
                    "django",
                    "rest_framework",
                    "flask",
                    "importlib",
                    "sys",
                    "os",
                }:
                    return False
                imported = importlib.import_module(name)
                path = getattr(imported, "__file__", None)
                if not path or not Path(path).resolve().is_relative_to(Path(sys.path[0]).resolve()):
                    return False
                if not isolated_module(imported, seen):
                    return False
        return True
    except (OSError, TypeError, ImportError, SyntaxError):
        return False


def _function(function: Any, allowed_globals: set[str]) -> ast.FunctionDef | None:
    node = ast.parse(textwrap.dedent(inspect.getsource(function))).body[0]
    if not isinstance(node, ast.FunctionDef) or node.decorator_list:
        return None
    if node.args.defaults or node.args.kwonlyargs or node.args.vararg or node.args.kwarg:
        return None
    bound = {arg.arg for arg in node.args.args}
    bound.update(
        n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
    )
    for item in ast.walk(node):
        if isinstance(item, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal)):
            return None
        if (
            isinstance(item, ast.Name)
            and isinstance(item.ctx, ast.Load)
            and (item.id == "self" or item.id not in bound | allowed_globals)
        ):
            return None
    return node


def authentication(view: Any) -> str | None:
    from types import SimpleNamespace

    from rest_framework.authentication import BaseAuthentication  # type: ignore[import-untyped]
    from rest_framework.exceptions import AuthenticationFailed  # type: ignore[import-untyped]

    if not view.authentication_classes:
        return ""
    if len(view.authentication_classes) != 1:
        return None
    auth = view.authentication_classes[0]
    if auth.__bases__ != (BaseAuthentication,):
        return None
    if any(
        not k.startswith("__") and k not in {"authenticate", "authenticate_header"}
        for k in vars(auth)
    ):
        return None
    methods = []
    for name in ("authenticate", "authenticate_header"):
        function = vars(auth).get(name)
        if function is None:
            return None
        for key, value in {
            "SimpleNamespace": SimpleNamespace,
            "AuthenticationFailed": AuthenticationFailed,
        }.items():
            if key in function.__globals__ and function.__globals__[key] is not value:
                return None
        node = _function(function, {"SimpleNamespace", "AuthenticationFailed"})
        if node is None or [a.arg for a in node.args.args] != ["self", "request"]:
            return None
        for item in ast.walk(node):
            if (
                isinstance(item, ast.Attribute)
                and isinstance(item.value, ast.Name)
                and item.value.id == "request"
                and item.attr not in {"headers", "method"}
            ):
                return None
        methods.append(ast.unparse(node))
    return "class _RouteAuth:\n" + textwrap.indent("\n".join(methods), "    ")


def serializer(name: str, value: Any) -> str | None:
    from rest_framework import serializers  # type: ignore[import-untyped]
    from rest_framework.settings import api_settings  # type: ignore[import-untyped]

    if not inspect.isclass(value) or value.__bases__ != (serializers.Serializer,):
        return None
    if any(
        not k.startswith("__") and k not in {"_declared_fields", "validate"} for k in vars(value)
    ):
        return None
    value = cast(Any, value)
    instance = value()
    fields = {}
    allowed = {
        "required",
        "allow_null",
        "allow_blank",
        "trim_whitespace",
        "min_length",
        "max_length",
        "min_value",
        "max_value",
        "error_messages",
    }
    for key, field in instance.fields.items():
        if (
            type(field) not in {serializers.CharField, serializers.IntegerField}
            or set(field._kwargs) - allowed
        ):
            return None
        fields[key] = {
            "kind": type(field).__name__,
            "required": field.required,
            "allow_null": field.allow_null,
            "messages": {k: str(v) for k, v in field.error_messages.items()},
            **{
                k: getattr(field, k)
                for k in (
                    "allow_blank",
                    "trim_whitespace",
                    "min_length",
                    "max_length",
                    "min_value",
                    "max_value",
                )
                if hasattr(field, k)
            },
        }
    validate = ""
    if "validate" in vars(value):
        function = value.validate
        if function.__globals__.get("serializers") is not serializers:
            return None
        node = _function(function, {"serializers"})
        if node is None or [a.arg for a in node.args.args] != ["self", "attrs"]:
            return None
        for item in ast.walk(node):
            if (
                isinstance(item, ast.Attribute)
                and isinstance(item.value, ast.Name)
                and item.value.id == "serializers"
            ):
                if item.attr != "ValidationError":
                    return None
                item.value.id = "_exceptions"
        validate = textwrap.indent(ast.unparse(node), "    ") + "\n"
    return (
        f"class {name}(_Input):\n    fields = {fields!r}\n"
        f"    non_field_errors = {api_settings.NON_FIELD_ERRORS_KEY!r}\n"
        f"    messages = { {k: str(v) for k, v in instance.error_messages.items()}!r}\n" + validate
    )

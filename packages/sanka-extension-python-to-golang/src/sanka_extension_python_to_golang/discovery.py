# SPDX-License-Identifier: Apache-2.0
"""Discover source inputs statically; destination choices remain Plan configuration."""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .capture import _python_path, normalize_frameworks, source_inventory
from .routing import project_tree


class InputRequired(ValueError):
    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


def _assignment(tree: ast.Module, name: str) -> ast.expr | None:
    values = [
        node.value
        for node in tree.body
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
        )
        or (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        )
    ]
    return values[0] if len(values) == 1 else None


def _dict_value(node: ast.expr | None, key: str) -> ast.expr | None:
    if not isinstance(node, ast.Dict):
        return None
    values = [
        value
        for name, value in zip(node.keys, node.values, strict=True)
        if isinstance(name, ast.Constant) and name.value == key
    ]
    return values[0] if len(values) == 1 else None


def _django(trees: dict[str, ast.Module]) -> dict[str, Any] | None:
    if "manage.py" not in trees:
        return None
    modules = [
        node.args[1].value
        for node in ast.walk(trees["manage.py"])
        if isinstance(node, ast.Call)
        and ast.unparse(node.func) == "os.environ.setdefault"
        and len(node.args) == 2
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "DJANGO_SETTINGS_MODULE"
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ]
    if len(modules) != 1:
        raise InputRequired(
            "source_file",
            "Django settings module is dynamic; choose an explicit source configuration",
        )
    settings = trees.get(_python_path(modules[0].replace(".", "/") + ".py", "settings module"))
    if settings is None:
        raise ValueError("Django settings must be a regular project Python file")
    urls = _assignment(settings, "ROOT_URLCONF")
    try:
        apps = ast.literal_eval(_assignment(settings, "INSTALLED_APPS"))  # type: ignore[arg-type]
    except (ValueError, TypeError):
        apps = []
    engine = _dict_value(_dict_value(_assignment(settings, "DATABASES"), "default"), "ENGINE")
    database = {
        "django.db.backends.sqlite3": "sqlite",
        "django.db.backends.postgresql": "postgresql",
    }.get(
        engine.value if isinstance(engine, ast.Constant) and isinstance(engine.value, str) else ""
    )
    return {
        "urls": urls.value
        if isinstance(urls, ast.Constant) and isinstance(urls.value, str)
        else None,
        "apps": [app.split(".apps.")[0].replace(".", "/") for app in apps if isinstance(app, str)]
        if isinstance(apps, (list, tuple))
        else [],
        "database": database,
    }


def _framework(tree: ast.Module) -> set[str]:
    imports = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    if "django" in imports and any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "urlpatterns" for target in node.targets
        )
        for node in tree.body
    ):
        imports.add("rest_framework")
    return {
        "drf" if name == "rest_framework" else name
        for name in imports & {"flask", "fastapi", "rest_framework"}
    }


def _url(node: ast.expr, tree: ast.Module) -> str | None:
    if isinstance(node, ast.Name):
        values = [
            item.value
            for item in tree.body
            if isinstance(item, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == node.id for target in item.targets
            )
        ]
        return (
            _url(values[0], tree)
            if len(values) == 1 and not isinstance(values[0], ast.Name)
            else None
        )
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if (
        isinstance(node, ast.Subscript)
        and ast.unparse(node.value) in {"environ", "os.environ"}
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    ):
        return os.environ.get(node.slice.value)
    if (
        isinstance(node, ast.Call)
        and ast.unparse(node.func) in {"environ.get", "os.environ.get", "os.getenv"}
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        fallback = _url(node.args[1], tree) if len(node.args) == 2 else None
        return os.environ.get(node.args[0].value, fallback)
    return None


def discover(root: Path, raw: dict[str, Any]) -> dict[str, Any]:
    values = {
        key: value for key, value in normalize_frameworks(raw).items() if value not in (None, "")
    }
    records, _ = source_inventory(root)
    trees = {}
    for name in records:
        if (
            not name.endswith(".py")
            or {"tests", "migrations", "versions"} & set(Path(name).parts)
            or Path(name).name.startswith("test_")
            or Path(name).name == "conftest.py"
        ):
            continue
        if (root / name).stat().st_size > 1024 * 1024:
            raise ValueError("discovery requires bounded Python files (at most 1 MiB each)")
        trees[name] = ast.parse((root / name).read_text(), filename=name)
    django = _django(trees) if values.get("source_framework", "auto") in {"auto", "drf"} else None
    if "source_file" not in values:
        if django and not django["urls"]:
            raise InputRequired(
                "source_file", "Choose the entrypoint; Django ROOT_URLCONF is dynamic"
            )
        candidates = (
            [django["urls"].replace(".", "/") + ".py"]
            if django
            else [
                name
                for name, tree in trees.items()
                if _framework(tree)
                and (
                    values.get("source_framework", "auto") == "auto"
                    or values["source_framework"] in _framework(tree)
                )
                and any(
                    isinstance(node, ast.Call)
                    and ast.unparse(node.func).split(".")[-1] in {"Flask", "FastAPI"}
                    for node in ast.walk(tree)
                )
            ]
        )
        # A standalone DRF URL module is also an entrypoint.
        if not django:
            candidates += [
                name
                for name, tree in trees.items()
                if "drf" in _framework(tree)
                and any(
                    isinstance(node, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == "urlpatterns"
                        for target in node.targets
                    )
                    for node in tree.body
                )
            ]
            # Prefer an application re-export over its implementation module.
            # Reuse capture's bounded import graph; unsafe facades still produce gaps.
            facades = []
            imported = set()
            for name, tree in trees.items():
                if name in candidates or not any(
                    isinstance(node, ast.ImportFrom)
                    and any(alias.name in {"app", "urlpatterns"} for alias in node.names)
                    for node in tree.body
                ):
                    continue
                try:
                    _, modules = project_tree(root, name, values.get("models_file", ""))
                except ValueError:
                    continue
                if set(modules) & set(candidates):
                    facades.append(name)
                    imported.update(modules)
            candidates = [name for name in candidates + facades if name not in imported]
        if len(candidates) != 1:
            raise InputRequired(
                "source_file", "Choose the Python entrypoint; no unique application was detected"
            )
        values["source_file"] = candidates[0]
    filename = _python_path(values["source_file"], "source_file")
    if filename not in trees:
        raise ValueError("source_file must be a regular project Python file")
    if values.get("source_framework", "auto") == "auto":
        frameworks = {"drf"} if django else _framework(trees[filename])
        if not frameworks:
            tree, _ = project_tree(root, filename, values.get("models_file", ""))
            frameworks = _framework(tree)
        if len(frameworks) != 1:
            raise InputRequired(
                "source_framework", "Choose drf, fastapi or flask; the entrypoint is ambiguous"
            )
        values["source_framework"] = next(iter(frameworks))
    if django and values["source_framework"] == "drf":
        models = [app + "/models.py" for app in django["apps"] if app + "/models.py" in trees]
        database = django["database"]
    else:
        models = [
            name
            for name, tree in trees.items()
            if any(
                isinstance(node, ast.ClassDef)
                and any(
                    isinstance(item, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == "__tablename__"
                        for target in item.targets
                    )
                    for item in node.body
                )
                for node in tree.body
            )
        ]
        if not models and (root / "models.py").is_file():
            models = ["models.py"]
        databases = set()
        for tree in trees.values():
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and ast.unparse(node.func).split(".")[-1]
                    in {"create_engine", "create_async_engine"}
                    and node.args
                ):
                    url = _url(node.args[0], tree)
                    if url:
                        scheme = urlsplit(url).scheme.split("+")[0]
                        databases.add(
                            {"postgres": "postgresql", "file": "sqlite"}.get(scheme, scheme)
                        )
        database = next(iter(databases)) if len(databases) == 1 else None
    source = values.get("source_database", "auto")
    if database and source != "auto" and source != database:
        raise ValueError("source_database differs from the detected source connection")
    if (
        models
        and not database
        and source == "auto"
        and raw.get("source_framework", "auto") in (None, "", "auto")
    ):
        # Retain legacy explicit framework/database recipes; automatic discovery never
        # infers a dynamic source connection from the selected destination.
        raise InputRequired(
            "source_database",
            "Choose the source database; its connection is dynamic or unsupported",
        )
    if values.get("database_layer", "auto") == "auto":
        if not models and not database:
            values["database_layer"] = "none"
        else:
            source = values.get("source_database", "auto")
            source = database if source == "auto" else source
            if source not in {"sqlite", "postgresql"}:
                raise InputRequired(
                    "source_database",
                    "Choose the source database; its connection is dynamic or unsupported",
                )
            values["database_layer"] = "sqlite" if source == "sqlite" else "pgx"
    if values["database_layer"] != "none":
        if values.get("source_database", "auto") == "auto" and database:
            values["source_database"] = database
        if "models_file" not in values:
            if not models or (not django and len(models) != 1):
                raise InputRequired(
                    "models_file",
                    "Choose the Python models file; no unique model module was detected",
                )
            values["models_file"] = models[0]
    elif values.get("source_database") == "auto":
        values.pop("source_database")
    return values

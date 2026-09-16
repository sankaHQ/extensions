# SPDX-License-Identifier: Apache-2.0
"""Static inventory of behavior outside the generated HTTP contracts.

This detects known hooks without importing source modules. It is an inventory,
not a sandbox or a claim that arbitrary Python can be understood statically.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        return _name(node.func)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _name(node.value) + "." + node.attr
    return ""


def inventory_behavior(root: Path) -> list[dict[str, str | int]]:
    findings: list[dict[str, str | int]] = []
    for directory, names, files in os.walk(root):
        names[:] = sorted(n for n in names if not n.startswith(".") and n != "__pycache__")
        for filename in sorted(files):
            path = Path(directory) / filename
            if path.suffix != ".py":
                continue
            relative = path.relative_to(root).as_posix()
            tree = ast.parse(path.read_text(), filename=relative)
            classes = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
            base_aliases = {
                "Model": {"Model"},
                "AppConfig": {"AppConfig"},
                "BaseCommand": {"BaseCommand"},
            }
            signal_names: set[str] = set()
            signal_modules: set[str] = set()
            for node in tree.body:
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    for imported in node.names:
                        local = imported.asname or imported.name
                        if imported.name in base_aliases:
                            base_aliases[imported.name].add(local)
                        if module.endswith(".signals"):
                            signal_names.add(local)
                        elif module == "django.db.models" and imported.name == "signals":
                            signal_modules.add(local)
                elif isinstance(node, ast.Import):
                    for imported in node.names:
                        if imported.name.endswith(".signals"):
                            signal_modules.add(imported.asname or imported.name.split(".")[0])

            def derives(
                node: ast.ClassDef,
                kind: str,
                seen: set[str] | None = None,
                *,
                _classes: dict[str, ast.ClassDef] = classes,
                _base_aliases: dict[str, set[str]] = base_aliases,
            ) -> bool:
                names = {_name(base).rsplit(".", 1)[-1] for base in node.bases}
                if names & _base_aliases[kind]:
                    return True
                visited = set() if seen is None else seen
                if node.name in visited:
                    return False
                return any(
                    name in _classes and derives(_classes[name], kind, visited | {node.name})
                    for name in names
                )

            def add(feature: str, node: ast.AST, source: str = relative) -> None:
                findings.append(
                    {"source": source, "line": getattr(node, "lineno", 0), "feature": feature}
                )

            for item in ast.walk(tree):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    decorators = {_name(d).rsplit(".", 1)[-1] for d in item.decorator_list}
                    if "receiver" in decorators:
                        add("signal-handler", item)
                    if decorators & {"task", "shared_task", "job", "periodic_task"}:
                        add("background-task", item)
                elif isinstance(item, ast.ClassDef):
                    for method in item.body:
                        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            continue
                        if derives(item, "Model") and method.name in {"save", "delete", "clean"}:
                            add("model-lifecycle-hook", method)
                        if derives(item, "AppConfig") and method.name == "ready":
                            add("application-startup-hook", method)
                    if derives(item, "BaseCommand"):
                        add("management-command", item)
                elif isinstance(item, ast.Call):
                    name = _name(item.func).rsplit(".", 1)[-1]
                    if "migrations" in path.parts and name in {"RunPython", "RunSQL"}:
                        add("data-migration", item)
                    if name == "connect" and isinstance(item.func, ast.Attribute):
                        signal = _name(item.func.value)
                        if (
                            signal in signal_names
                            or any(
                                signal == module or signal.startswith(module + ".")
                                for module in signal_modules
                            )
                            or ".signals." in signal
                        ):
                            add("signal-handler", item)
    return sorted(findings, key=lambda item: (item["source"], item["line"], item["feature"]))

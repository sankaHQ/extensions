# SPDX-License-Identifier: Apache-2.0
"""Render destination-owned code without importing either provider SDK."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from .common import canonical, safe_path


def render_candidate(root: Path, spec: dict[str, Any], inventory: dict[str, Any]) -> dict[str, str]:
    site = next(
        item for item in inventory["call_sites"] if item["id"] == spec["source"]["call_site"]
    )
    if site["status"] != "supported":
        raise ValueError("; ".join(site["reasons"]))
    decision_id = spec["decision_id"]
    module = f"jev_adapter_{decision_id}"
    config = f"jev_decision_{decision_id}.json"
    test = f"test_jev_{decision_id}.py"
    source_path = safe_path(root, site["file"])
    source = source_path.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    argument = site["input_name"]
    replacement = (
        f"def {site['function']}({argument}: str) -> str:\n"
        f"    from {module} import classify as _jev_classify\n\n"
        f"    return _jev_classify({argument})\n"
    )
    lines[site["function_line"] - 1 : site["function_end_line"]] = [replacement]
    changed_source = "".join(lines)
    # Remove only the now-unreferenced exact source client binding. Other call sites
    # and dependencies remain untouched, including manual OpenAI consumers.
    tree = ast.parse(changed_source)
    loads = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    removals: set[int] = set()
    if "client" not in loads:
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "client"
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "OpenAI"
            ):
                if any(
                    other is not node
                    and other.lineno <= (node.end_lineno or node.lineno)
                    and (other.end_lineno or other.lineno) >= node.lineno
                    for other in tree.body
                ):
                    raise ValueError("shared-line client statements require manual migration")
                removals.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        changed_source = "".join(
            line
            for number, line in enumerate(changed_source.splitlines(keepends=True), 1)
            if number not in removals
        )
        tree = ast.parse(changed_source)
        if not any(isinstance(node, ast.Name) and node.id == "OpenAI" for node in ast.walk(tree)):
            for node in tree.body:
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module == "openai"
                    and any(
                        other is not node
                        and other.lineno <= (node.end_lineno or node.lineno)
                        and (other.end_lineno or other.lineno) >= node.lineno
                        for other in tree.body
                    )
                ):
                    raise ValueError("shared-line OpenAI imports require manual migration")
            removals = {
                number
                for node in tree.body
                if isinstance(node, ast.ImportFrom)
                and node.module == "openai"
                and len(node.names) == 1
                and node.names[0].name == "OpenAI"
                for number in range(node.lineno, (node.end_lineno or node.lineno) + 1)
            }
            changed_source = "".join(
                line
                for number, line in enumerate(changed_source.splitlines(keepends=True), 1)
                if number not in removals
            )
    templates = Path(__file__).parent / "templates"
    adapter = (templates / "adapter.py.tmpl").read_text().replace("__CONFIG_FILE__", repr(config))
    tests = (templates / "compatibility.py.tmpl").read_text().replace("__ADAPTER_MODULE__", module)
    tests = tests.replace("__SOURCE_FILE__", repr(site["file"]))
    tests = tests.replace("__SOURCE_FUNCTION__", repr(site["function"]))
    generated = {
        site["file"]: changed_source,
        f"{module}.py": adapter,
        config: canonical(spec),
        test: tests,
        "requirements-jev.txt": "# Destination-owned inference dependency.\ntypesafe-sdk==0.7.0\n",
    }
    for relative in generated:
        target = safe_path(root, relative)
        if relative != site["file"] and target.exists():
            raise ValueError(f"generated path already exists: {relative}")
    return generated

# SPDX-License-Identifier: Apache-2.0
"""Render only the supported, typed capture grammar into independent Python."""

from __future__ import annotations

from pathlib import Path
from typing import Any

LOCK = Path(__file__).parent / "requirements.lock"


def value(node: dict[str, Any], records: dict[str, Any]) -> str:
    kind = node["kind"]
    if kind == "null":
        return "None"
    if kind in {"string", "boolean", "integer"}:
        return repr(node["value"])
    if kind == "name":
        return "_p_" + str(node["value"])
    if kind == "plus":
        return f"({value(node['left'], records)} + {value(node['right'], records)})"
    args = [value(item, records) for item in node["args"]]
    if kind == "map":
        return (
            "{" + ", ".join(f"{k}: {v}" for k, v in zip(args[::2], args[1::2], strict=True)) + "}"
        )
    if kind == "list":
        return "[" + ", ".join(args) + "]"
    if kind == "record":
        fields = records[node["type"]]
        return (
            str(node["type"])
            + "("
            + ", ".join(f"{f['name']}={arg}" for f, arg in zip(fields, args, strict=True))
            + ")"
        )
    raise ValueError(f"cannot render expression {kind}")


def render(captured: dict[str, Any], scenarios: list[dict[str, Any]]) -> dict[str, str]:
    if "persistence" in captured:
        from .crud_render import render_crud

        return render_crud(captured, scenarios)
    lines = [
        "# SPDX-License-Identifier: Apache-2.0",
        "from fastapi import FastAPI, Request",
        "from pydantic import BaseModel",
        "",
        "app = FastAPI(redirect_slashes=False)",
        "",
        "def _query(request: Request, name: str, default: str) -> str:",
        "    values = request.query_params.getlist(name)",
        "    if not values or values == ['']:",
        "        return default",
        "    return ','.join(values)",
    ]
    for name, fields in sorted(captured["records"].items()):
        lines.extend(["", f"class {name}(BaseModel):"])
        for field in fields:
            kind = {"String": "str | None", "int": "int", "long": "int", "boolean": "bool"}[
                field["type"]
            ]
            lines.append(f"    {field['name']}: {kind}")
        if not fields:
            lines.append("    pass")
    # Spring chooses literal routes before path variables regardless of declaration order.
    endpoints = sorted(captured["endpoints"], key=lambda e: (e["path"].count("{"), e["path"]))
    for index, endpoint in enumerate(endpoints):
        model = endpoint["response"].get("type", "None")
        lines.extend(
            [
                "",
                f"@app.get({endpoint['path']!r}, response_model={model})",
                f"def endpoint_{index}(request: Request):",
            ]
        )
        for param in endpoint["parameters"]:
            expr = (
                f"_query(request, {param['external']!r}, {param['default']!r})"
                if param["binding"] == "RequestParam"
                else f"request.path_params[{param['external']!r}]"
            )
            lines.append(f"    _p_{param['name']} = {expr}")
        lines.append("    return " + value(endpoint["response"], captured["records"]))
    import json

    return {
        "app.py": "\n".join(lines) + "\n",
        "requirements.lock": LOCK.read_text(),
        "scenarios.json": json.dumps(scenarios, indent=2) + "\n",
        "test_app.py": (Path(__file__).parent / "runners" / "test_candidate.py.txt").read_text(),
        "README.md": (
            "# Generated FastAPI candidate\n\n"
            "Install in a dedicated environment with "
            "`uv pip sync --require-hashes requirements.lock`. "
            "Run `python -m pytest` and `python -m uvicorn app:app`. "
            "No Java or Sanka runtime is required to serve requests. "
            "Only the recorded GET JSON cases have been qualified; "
            "review the migration report before deployment.\n"
        ),
    }

# SPDX-License-Identifier: Apache-2.0
"""Emit native handlers from the closed PHP capture; no source-code evaluation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .capture import canonical

TEMPLATES = Path(__file__).parent / "templates"
PYTHON = "3.12.13"
GO = "1.27.0"


class PythonEmitter:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.counter = 0

    def expression(self, value: dict[str, Any], indent: int) -> str:
        op = value["op"]

        def expr(item: dict[str, Any]) -> str:
            return self.expression(item, indent)

        if op == "literal":
            return repr(value["value"])
        if op == "variable":
            return "v_" + str(value["name"])
        if op == "array":
            return (
                "php_array(["
                + ", ".join(f"({i}, {expr(item)})" for i, item in enumerate(value["items"]))
                + "])"
            )
        if op == "object":
            return (
                "php_array(["
                + ", ".join(
                    "(" + repr(key) + ", " + expr(item) + ")" for key, item in value["items"]
                )
                + "])"
            )
        if op == "index":
            return f"index({expr(value['object'])}, {expr(value['key'])})"
        if op == "not":
            return f"(not truth({expr(value['value'])}))"
        if op == "binary":
            left, right, operator = expr(value["left"]), expr(value["right"]), value["operator"]
            if operator in {"===", "!=="}:
                return f"({'not ' if operator == '!==' else ''}identical({left}, {right}))"
            if operator == "??":
                return f"coalesce(lambda: {left}, lambda: {right})"
            return f"(truth({left}) {'and' if operator == '&&' else 'or'} truth({right}))"
        if op in {"env", "getenv"}:
            return f"environment({value['name']!r}, {op == 'env'})"
        if op == "hash_equals":
            return f"secure_equal({expr(value['left'])}, {expr(value['right'])})"
        if op == "request":
            return (
                f"request_value(ctx, {value['kind']!r}, {value['key']!r}, {expr(value['default'])})"
            )
        if op == "bearer":
            return "bearer(ctx)"
        if op == "validate":
            return f"validate(ctx, {expr(value['rules'])})"
        if op == "response":
            return "{'status': " + expr(value["status"]) + ", 'body': " + expr(value["body"]) + "}"
        if op == "sql":
            return (
                f"db.query({value['kind']!r}, {value['sql']!r}, "
                f"list({expr(value['params'])}.values()))"
            )
        if op == "transaction":
            self.counter += 1
            name = f"transaction_{self.counter}"
            captures = "".join(f", v_{key}=v_{key}" for key in value["captures"])
            self.lines.append("    " * indent + f"def {name}(db{captures}):")
            self.block(value["body"], indent + 1)
            return f"db.transaction({name})"
        raise ValueError(f"unsupported captured expression {op}")

    def block(self, statements: list[dict[str, Any]], indent: int) -> None:
        for statement in statements:
            prefix = "    " * indent
            op = statement["op"]
            if op == "if":
                condition = self.expression(statement["condition"], indent)
                self.lines.append(prefix + f"if truth({condition}):")
                self.block(statement["then"], indent + 1)
                if not statement["then"]:
                    self.lines.append(prefix + "    pass")
                if statement["else"]:
                    self.lines.append(prefix + "else:")
                    self.block(statement["else"], indent + 1)
            else:
                value = self.expression(statement["value"], indent)
                if op == "throw":
                    self.lines.append(prefix + f"raise RuntimeError({value})")
                    continue
                action = {
                    "return": "return ",
                    "expression": "",
                    "assign": "v_" + statement.get("name", "") + " = ",
                }[op]
                self.lines.append(prefix + action + value)


def render(captured: dict[str, Any], target: str) -> dict[str, str]:
    if captured["gaps"] or any(route["gaps"] for route in captured["routes"]):
        raise ValueError("selected PHP behavior has blockers; generation is unavailable")
    if target == "chi":
        from .render_go import render_go

        return render_go(captured)
    if target != "fastapi":
        raise ValueError("target must be fastapi or chi")
    emitter = PythonEmitter()
    emitter.lines.append(
        "# SPDX-License-Identifier: Apache-2.0\nfrom php_compat import (\n"
        "    truth, identical, index, coalesce, secure_equal, environment,\n"
        "    request_value, bearer, validate, php_array,\n)\n"
    )
    registrations = []
    for number, route in enumerate(captured["routes"]):
        name = f"route_{number}"
        emitter.lines.append(f"def {name}(ctx, db):")
        names = route["parameters"]
        emitter.lines.append(f"    v_{names[0]} = ctx")
        for parameter in names[1:]:
            emitter.lines.append(f"    v_{parameter} = ctx['params'][{parameter!r}]")
        emitter.block(route["body"], 1)
        emitter.lines.append("")
        registrations.append(f"({route['method']!r}, {route['path']!r}, {name})")
    app = (
        "# SPDX-License-Identifier: Apache-2.0\nfrom handlers import "
        + ", ".join(f"route_{i}" for i in range(len(registrations)))
        + "\nfrom php_compat import make_app\n\napp = make_app([\n    "
        + ",\n    ".join(registrations)
        + f"\n], {captured['kind']!r}, {captured['database']['dialect']!r})\n"
    )
    lock = (
        Path(__file__).parent
        / "locks"
        / ("python-mysql" if captured["database"]["dialect"] == "mysql" else "python")
    )
    return {
        "app.py": app,
        "handlers.py": "\n".join(emitter.lines) + "\n",
        "php_compat.py": (TEMPLATES / "python_compat.py.txt").read_text(),
        "requirements.in": (lock / "requirements.in").read_text(),
        "requirements.txt": (lock / "requirements.txt").read_text(),
        ".python-version": PYTHON + "\n",
        "contract.json": canonical(captured) + "\n",
        "README.md": (
            "# Generated FastAPI API\n\nReview `handlers.py` before adoption. "
            "Install the locked requirements and run\n"
            "`uvicorn app:app --host 127.0.0.1 --port 8000 "
            "--timeout-keep-alive 5 --timeout-graceful-shutdown 15`.\n\n"
            "Set DATABASE_URL for the captured database engine. Schema adoption and production\n"
            "traffic changes are separate operations. Run Sanka Test and Verify in a qualified\n"
            "Linux sandbox with disposable fixtures; generation does not prove equivalence.\n"
        ),
        "routes.json": json.dumps(
            [{"method": r["method"], "path": r["path"]} for r in captured["routes"]], indent=2
        )
        + "\n",
    }

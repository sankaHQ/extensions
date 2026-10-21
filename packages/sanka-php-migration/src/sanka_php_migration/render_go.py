# SPDX-License-Identifier: Apache-2.0
"""Native chi handlers over database/sql, with bounded PHP compatibility helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .capture import canonical

TEMPLATES = Path(__file__).parent / "templates"
LOCKS = Path(__file__).parent / "locks"


def quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


class GoEmitter:
    def expression(self, value: dict[str, Any]) -> str:
        expr = self.expression
        op = value["op"]
        if op == "literal":
            item = value["value"]
            if item is None:
                return "nil"
            if isinstance(item, bool):
                return str(item).lower()
            if isinstance(item, int):
                return f"int64({item})"
            return quote(item)
        if op == "variable":
            return "v_" + str(value["name"])
        if op in {"array", "object"}:
            entries = enumerate(value["items"]) if op == "array" else value["items"]
            return (
                "array("
                + ", ".join(
                    "entry{"
                    + (f"int64({key})" if isinstance(key, int) else quote(key))
                    + ", "
                    + expr(item)
                    + "}"
                    for key, item in entries
                )
                + ")"
            )
        if op == "index":
            return f"index({expr(value['object'])}, {expr(value['key'])})"
        if op == "not":
            return f"(!truth({expr(value['value'])}))"
        if op == "binary":
            left, right, operator = expr(value["left"]), expr(value["right"]), value["operator"]
            if operator in {"===", "!=="}:
                return f"({'!' if operator == '!==' else ''}identical({left}, {right}))"
            if operator == "??":
                return f"coalesce(func() any {{ return {left} }}, func() any {{ return {right} }})"
            return f"(truth({left}) {operator} truth({right}))"
        if op in {"env", "getenv"}:
            return f"environment({quote(value['name'])}, {str(op == 'env').lower()})"
        if op == "hash_equals":
            return f"secureEqual({expr(value['left'])}, {expr(value['right'])})"
        if op == "request":
            return (
                f"requestValue(ctx, {quote(value['kind'])}, {quote(value['key'])}, "
                f"{expr(value['default'])})"
            )
        if op == "bearer":
            return "bearer(ctx)"
        if op == "validate":
            return f"validate(ctx, {expr(value['rules'])})"
        if op == "response":
            return (
                f'array(entry{{"status", {expr(value["status"])}}}, '
                f'entry{{"body", {expr(value["body"])}}})'
            )
        if op == "sql":
            return (
                f"db.query({quote(value['kind'])}, {quote(value['sql'])}, {expr(value['params'])})"
            )
        if op == "transaction":
            return (
                "db.transaction(func(db *database) any {\n"
                + "\n".join(
                    f"var v_{name} any = v_{name}; _ = v_{name}" for name in value["captures"]
                )
                + "\n"
                + self.declarations(value["body"], value["captures"])
                + self.block(value["body"])
                + "\n})"
            )
        raise ValueError(f"unsupported captured expression {op}")

    def declarations(self, statements: list[dict[str, Any]], inherited: list[str]) -> str:
        names: set[str] = set()

        def visit(items: list[dict[str, Any]]) -> None:
            for item in items:
                if item["op"] == "assign":
                    names.add(item["name"])
                elif item["op"] == "if":
                    visit(item["then"])
                    visit(item["else"])

        visit(statements)
        return (
            "\n".join(f"var v_{name} any; _ = v_{name}" for name in sorted(names - set(inherited)))
            + "\n"
        )

    def block(self, statements: list[dict[str, Any]]) -> str:
        lines = []
        for item in statements:
            op = item["op"]
            if op == "if":
                line = (
                    f"if truth({self.expression(item['condition'])}) {{\n"
                    f"{self.block(item['then'])}\n}}"
                )
                if item["else"]:
                    line += f" else {{\n{self.block(item['else'])}\n}}"
                lines.append(line)
            else:
                value = self.expression(item["value"])
                if op == "throw":
                    lines.append(f"panic({value})")
                    continue
                action = {
                    "return": "return ",
                    "expression": "_ = ",
                    "assign": "v_" + item.get("name", "") + " = ",
                }[op]
                lines.append(action + value)
        return "\n".join(lines)


def render_go(captured: dict[str, Any]) -> dict[str, str]:
    dialect = captured["database"]["dialect"]
    emitter = GoEmitter()
    lines = [
        "// SPDX-License-Identifier: Apache-2.0\npackage migrated\n"
        'import ("net/http"; "github.com/go-chi/chi/v5")\n'
    ]
    registers = []
    for i, route in enumerate(captured["routes"]):
        lines.append(f"func route{i}(ctx *phpArray, db *database) any {{")
        for j, name in enumerate(route["parameters"]):
            value = "ctx" if j == 0 else f'index(index(ctx, "params"), {quote(name)})'
            lines.append(f"var v_{name} any = {value}; _ = v_{name}")
        lines.append(emitter.declarations(route["body"], route["parameters"]))
        lines.append(emitter.block(route["body"]))
        lines.append("}\n")
        registers.append(
            f"router.MethodFunc({quote(route['method'])}, {quote(route['path'])}, "
            f"wrap(pool, route{i}, {quote(captured['kind'])}))"
        )
    lines.append(
        f"func NewApp() (http.Handler, func()) {{\npool := openDatabase({quote(dialect)})\n"
        "router := chi.NewRouter()"
    )
    lines.extend(registers)
    lines.append("return router, func() { if pool != nil { _ = pool.Close() } }\n}\n")
    drivers = ""
    database = '    _ = raw\n    panic("unsupported database engine")'
    if dialect == "sqlite":
        drivers = '    _ "github.com/mattn/go-sqlite3"'
        database = """    if !strings.HasPrefix(raw, "sqlite:///") {
        panic("DATABASE_URL must be sqlite:///<path>")
    }
    driver = "sqlite3"
    dsn = strings.TrimPrefix(raw, "sqlite:///") + "?_foreign_keys=on&_busy_timeout=5000"
"""
    elif dialect == "mysql":
        drivers = '    "net/url"\n    "github.com/go-sql-driver/mysql"'
        database = """    parsed, err := url.Parse(raw)
    if err != nil || parsed.Scheme != "mysql" || parsed.User == nil ||
        parsed.Host == "" || parsed.RawQuery != "" {
        panic("DATABASE_URL must be mysql://user:password@host/database")
    }
    config := mysql.NewConfig(); config.Net = "tcp"; config.Addr = parsed.Host
    if socket := os.Getenv("DATABASE_SOCKET"); socket != "" {
        config.Net = "unix"; config.Addr = socket
    }
    config.User = parsed.User.Username(); config.Passwd, _ = parsed.User.Password()
    config.DBName = strings.TrimPrefix(parsed.Path, "/"); config.ParseTime = false
    config.Timeout = 5*time.Second
    config.ReadTimeout = 15*time.Second; config.WriteTimeout = 15*time.Second
    driver, dsn = "mysql", config.FormatDSN()"""
    runtime = (
        (TEMPLATES / "go_runtime.go.txt")
        .read_text()
        .replace("@DRIVERS@", drivers)
        .replace("@DATABASE@", database)
    )
    return {
        "handlers.go": "\n".join(lines),
        "runtime.go": runtime,
        "cmd/api/main.go": (TEMPLATES / "go_main.go.txt").read_text(),
        "go.mod": (LOCKS / dialect / "go.mod").read_text(),
        "go.sum": (LOCKS / dialect / "go.sum").read_text(),
        "contract.json": canonical(captured) + "\n",
        "README.md": (
            "# Generated Go API\n\nReview `handlers.go` and "
            "run `go build -mod=readonly ./cmd/api`.\n"
            "Set DATABASE_URL for the captured engine; run the resulting binary.\n"
            "Sanka Test and Verify require a qualified Linux sandbox with disposable fixtures.\n"
            "Generation does not establish parity or authorize production traffic changes.\n"
        ),
    }

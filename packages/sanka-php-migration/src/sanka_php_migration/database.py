# SPDX-License-Identifier: Apache-2.0
"""Closed schema parsing for isolated replay. Production schema is never applied."""

from __future__ import annotations

import re
from typing import Any

IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"


def schema_tables(source: str, dialect: str, expected: list[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    statements = [part.strip() for part in source.split(";") if part.strip()]
    for statement in statements:
        match = re.fullmatch(
            rf"CREATE\s+TABLE\s+({IDENTIFIER})\s*\((.*)\)(?:\s+ENGINE=InnoDB)?",
            statement,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match is None:
            raise ValueError("schema supports only flat CREATE TABLE statements")
        table, raw = match.groups()
        if table not in expected or any(item["name"] == table for item in result):
            raise ValueError("schema contains an undeclared or duplicate table")
        definitions = re.split(r",(?![^()]*\))", raw)
        columns, primary, automatic = [], "", False
        for definition in definitions:
            column = re.fullmatch(
                rf"\s*({IDENTIFIER})\s+"
                r"(BIGINT|INTEGER|INT|TEXT|BOOLEAN|VARCHAR\([1-9][0-9]{0,3}\)|"
                r"DECIMAL\([1-9][0-9]?,[0-9]{1,2}\))"
                r"(\s+(?:(?:NOT\s+NULL|NULL|PRIMARY\s+KEY|AUTO_INCREMENT|AUTOINCREMENT|UNIQUE)\s*)*)?",
                definition,
                flags=re.IGNORECASE,
            )
            if column is None:
                raise ValueError("schema column type, constraint or default requires adaptation")
            name, kind, suffix = column.groups()
            if name in columns:
                raise ValueError("schema contains a duplicate column")
            columns.append(name)
            flags = (suffix or "").upper()
            if "PRIMARY" in flags:
                if primary or kind.upper() not in {"BIGINT", "INTEGER", "INT"}:
                    raise ValueError("schema needs one integer primary key per table")
                primary = name
                automatic = "AUTO_INCREMENT" in flags or "AUTOINCREMENT" in flags
            elif "AUTO" in flags:
                raise ValueError("automatic identity must be the primary key")
            if dialect == "mysql" and "AUTOINCREMENT" in flags:
                raise ValueError("SQLite identity syntax cannot be used for MySQL")
            if dialect == "sqlite" and "AUTO_INCREMENT" in flags:
                raise ValueError("MySQL identity syntax cannot be used for SQLite")
            if dialect == "sqlite" and "AUTOINCREMENT" in flags and kind.upper() != "INTEGER":
                raise ValueError("SQLite AUTOINCREMENT requires INTEGER PRIMARY KEY")
        if not primary:
            raise ValueError("each observed table needs an explicit primary key")
        result.append(
            {
                "name": table,
                "columns": columns,
                "primary_key": primary,
                "auto": automatic,
                "statement": statement,
            }
        )
    if set(expected) != {item["name"] for item in result}:
        raise ValueError("schema is missing a declared table")
    return result


def used_tables(value: Any) -> set[str]:
    if isinstance(value, dict):
        own = {str(value["table"])} if value.get("op") == "sql" else set()
        return own.union(*(used_tables(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(used_tables(item) for item in value))
    return set()

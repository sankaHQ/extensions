# SPDX-License-Identifier: Apache-2.0
"""Application deletion semantics for the captured, acyclic model graph."""

from __future__ import annotations

from collections import deque
from typing import Any

import sqlalchemy as sa


class DeletionBlocked(Exception):
    """The source model's PROTECT or RESTRICT policy prevents deletion."""


def delete_instance(
    session: Any,
    tables: dict[str, Any],
    schema: dict[str, Any],
    table_name: str,
    instance: Any,
) -> None:
    """Collect first, then mutate inside the caller's transaction.

    No SQL ON DELETE shortcuts: Django's collector owns these effects in Python.
    Custom signals, SET_DEFAULT and cyclic inter-table graphs must be blocked by
    qualification. DO_NOTHING remains a database constraint check at commit.
    """
    specs = {table["name"]: table for table in schema["tables"]}
    primary_keys = {
        name: next(column["name"] for column in table["columns"] if column["primary_key"])
        for name, table in specs.items()
    }
    collected: dict[str, dict[Any, Any]] = {name: {} for name in specs}
    queue = deque([(table_name, instance)])
    restricted: list[tuple[str, Any]] = []
    nullable: list[tuple[str, Any, str]] = []
    while queue:
        name, row = queue.popleft()
        key = row[primary_keys[name]]
        if key in collected[name]:
            continue
        collected[name][key] = row
        for child_name, child in specs.items():
            for column in child["columns"]:
                reference = column.get("references")
                if not reference or reference["table"] != name:
                    continue
                policy = reference["on_delete"]
                if policy == "DO_NOTHING":
                    continue
                child_table = tables[child_name]
                query = sa.select(child_table).where(
                    child_table.c[column["name"]] == row[reference["column"]]
                )
                children = list(session.execute(query).mappings())
                if policy == "CASCADE":
                    queue.extend((child_name, child_row) for child_row in children)
                elif policy == "PROTECT" and children:
                    raise DeletionBlocked("protected related objects")
                elif policy == "RESTRICT":
                    restricted.extend(
                        (child_name, child_row[primary_keys[child_name]]) for child_row in children
                    )
                elif policy == "SET_NULL":
                    nullable.extend(
                        (child_name, child_row[primary_keys[child_name]], column["name"])
                        for child_row in children
                    )
                elif policy not in {"PROTECT", "RESTRICT"}:
                    raise ValueError("unsupported deletion policy: " + policy)
    if any(key not in collected[name] for name, key in restricted):
        raise DeletionBlocked("restricted related objects")
    for name, key, column in nullable:
        if key not in collected[name]:
            session.execute(
                sa.update(tables[name])
                .where(tables[name].c[primary_keys[name]] == key)
                .values({column: None})
            )
    # Delete referring tables before referenced tables; self references are deleted
    # in one statement and don't create an inter-table dependency.
    remaining = {name for name, rows in collected.items() if rows}
    while remaining:
        referenced = {
            column["references"]["table"]
            for name in remaining
            for column in specs[name]["columns"]
            if column.get("references")
            and column["references"]["table"] in remaining
            and column["references"]["table"] != name
        }
        leaves = sorted(remaining - referenced)
        if not leaves:
            raise ValueError("cyclic deletion graph requires unsupported deferred constraints")
        for name in leaves:
            session.execute(
                sa.delete(tables[name]).where(
                    tables[name].c[primary_keys[name]].in_(collected[name])
                )
            )
            remaining.remove(name)

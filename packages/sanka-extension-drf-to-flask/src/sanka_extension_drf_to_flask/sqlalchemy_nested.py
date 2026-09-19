# SPDX-License-Identifier: Apache-2.0
"""Execute the captured atomic parent/child create operation."""

from __future__ import annotations

import operator
from typing import Any

import sqlalchemy as sa

from .native_runtime import _ValidationError


def create_nested(session: Any, tables: Any, resource: Any, values: dict[str, Any]) -> Any:
    field = next(f for f in resource["fields"] if f["kind"] == "nested_many" and not f["read_only"])
    values = dict(values)
    children = values.pop(field["name"])
    table = tables[resource["db_table"]]
    key = session.execute(sa.insert(table).values(**values)).inserted_primary_key[0]
    child = field["child"]
    child_table = tables[child["db_table"]]
    for item in children:
        session.execute(sa.insert(child_table).values(**item, **{field["foreign_key"]: key}))
    rule = resource["create_contract"].get("rule")
    if rule:
        column = next(c["name"] for c in child["columns"] if c["attribute"] == rule["attribute"])
        total = session.execute(
            sa.select(sa.func.coalesce(sa.func.sum(child_table.c[column]), 0)).where(
                child_table.c[field["foreign_key"]] == key
            )
        ).scalar_one()
        compare = {
            "Gt": operator.gt,
            "GtE": operator.ge,
            "Lt": operator.lt,
            "LtE": operator.le,
            "Eq": operator.eq,
            "NotEq": operator.ne,
        }[rule["operator"]]
        if compare(total, rule["limit"]):
            raise _ValidationError(rule["detail"])
    return key

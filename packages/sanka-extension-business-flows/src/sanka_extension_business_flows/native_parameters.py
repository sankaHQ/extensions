# SPDX-License-Identifier: Apache-2.0
"""Resolve initial input using the existing native recipes' distinct ID rules.

This has no workspace lookup. The hosted adapter must separately authorize all
selected records and members. Keep these effective settings separate from the
original catalog-resolved values used by existing generation request receipts.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sanka_extension_business_flows.catalog import resolve_parameters
from sanka_extension_business_flows.native_recipes import native_recipe


def _member_ids(values: list[str], *, approvals: bool = False) -> list[str]:
    if any(
        not value.isdecimal()
        or (not approvals and not value.isascii())
        or not 0 < int(value) < 2**63
        for value in values
    ):
        raise ValueError("Select positive workspace-user IDs")
    normalized = [str(int(value)) for value in values]
    if approvals and len(set(normalized)) != len(normalized):
        raise ValueError("Approval selections contain duplicate workspace users")
    return list(dict.fromkeys(normalized))


def _ticket_assignees(values: list[str]) -> list[str]:
    normalized = [value.strip() for value in values]
    if any(
        re.fullmatch(r"[1-9][0-9]*", value) is None or int(value) >= 2**63 for value in normalized
    ):
        raise ValueError("Ticket assignment requires canonical workspace-user IDs")
    if len(set(normalized)) != len(normalized):
        raise ValueError("Ticket assignment contains duplicate workspace users")
    return sorted(normalized, key=int)


def _names(values: list[str], *, maximum: int, reject_collisions: bool = False) -> list[str]:
    # Check the native limit before normalizing aliases, as native validators do.
    if len(values) > maximum:
        raise ValueError(f"Select at most {maximum} names")
    normalized = [value.strip() for value in values]
    if reject_collisions and len(set(normalized)) != len(normalized):
        raise ValueError("Task titles collide after trimming")
    return list(dict.fromkeys(normalized))


def _price(value: str) -> str:
    if not 1 <= len(value) <= 30:
        raise ValueError("unit_price requires at most 30 characters")
    try:
        number = Decimal(value)
        if not number.is_finite() or not 0 <= number <= 1_000_000_000:
            raise ValueError("unit_price is outside the supported range")
        exponent = number.as_tuple().exponent
        if type(exponent) is not int or not -6 <= exponent <= 29:
            # Bound before expanding exponent notation, including zero values.
            raise ValueError("unit_price requires bounded fixed notation with six decimal places")
        result = format(number, "f")
        if len(result) > 30:
            raise ValueError("unit_price requires at most 30 fixed-notation characters")
        return result
    except InvalidOperation as exc:
        raise ValueError("unit_price requires a finite decimal number") from exc


def resolve_native_parameters(recipe_id: str, supplied: dict[str, Any]) -> dict[str, Any]:
    """Normalize copies of resolved initial input; profile validation follows."""
    selected = native_recipe(recipe_id)
    settings = resolve_parameters(recipe_id, supplied)
    if selected.family == "approval":
        settings["approver_ids"] = _member_ids(settings["approver_ids"], approvals=True)
    if "assignee_ids" in settings:
        settings["assignee_ids"] = (
            _ticket_assignees(settings["assignee_ids"])
            if selected.variant == "ticket_assignment"
            else _member_ids(settings["assignee_ids"])
        )
    if "employee_ids" in settings:
        settings["employee_ids"] = _member_ids(settings["employee_ids"])
    if "open_stages" in settings:
        settings["open_stages"] = list(
            dict.fromkeys(str(UUID(value)) for value in settings["open_stages"])
        )
    for key in ("inventory_id", "supplier_id"):
        if key in settings:
            settings[key] = str(UUID(settings[key]))
    for key in ("task_titles", "task_names", "open_statuses"):
        if key in settings:
            settings[key] = _names(
                settings[key],
                maximum=50 if key == "open_statuses" else 20,
                reject_collisions=key == "task_titles",
            )
    if "unit_price" in settings:
        settings["unit_price"] = _price(settings["unit_price"])
    return settings

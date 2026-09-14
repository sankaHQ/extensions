# SPDX-License-Identifier: Apache-2.0
"""Versioned settings shared by manual configuration, AI proposals and generators."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any, cast


def catalog() -> dict[str, Any]:
    """Return an independent copy of the package's static business definitions."""
    return cast(
        dict[str, Any],
        json.loads(files(__package__).joinpath("catalog.json").read_text(encoding="utf-8")),
    )


def recipe(recipe_id: str) -> dict[str, Any]:
    for item in catalog()["recipes"]:
        if item["id"] == recipe_id:
            return cast(dict[str, Any], item)
    raise ValueError("Unknown business recipe")


def resolve_parameters(
    recipe_id: str, supplied: dict[str, Any], *, require_complete: bool = True
) -> dict[str, Any]:
    """Validate settings without coercions or workspace/provider lookups."""
    known = {field["key"]: field for field in recipe(recipe_id)["parameters"]}
    if type(supplied) is not dict or supplied.keys() - known.keys():
        raise ValueError("Unknown template configuration fields")
    resolved: dict[str, Any] = {}
    for key, field in known.items():
        value = supplied.get(key, field["default"])
        if value is None:
            value = field["default"]
        if value is None:
            if field["required"] and require_complete:
                raise ValueError(f"{key} is required")
            resolved[key] = None
            continue
        kind = field["kind"]
        if kind == "integer":
            if (
                type(value) is not int
                or (field["minimum"] is not None and value < field["minimum"])
                or (field["maximum"] is not None and value > field["maximum"])
            ):
                raise ValueError(f"{key} is outside the supported range")
        elif kind == "boolean":
            if type(value) is not bool:
                raise ValueError(f"{key} must be a boolean")
        elif kind == "string_list":
            if (
                not isinstance(value, list)
                or len(value) > 100
                or any(
                    not isinstance(item, str) or not item.strip() or len(item) > 200
                    for item in value
                )
            ):
                raise ValueError(f"{key} must contain at most 100 nonempty values")
            value = list(dict.fromkeys(value))
        elif not isinstance(value, str) or len(value) > 2000:
            raise ValueError(f"{key} must be text of at most 2000 characters")
        elif kind == "choice" and value not in {option["value"] for option in field["options"]}:
            raise ValueError(f"{key} is not a supported option")
        if (
            field["required"]
            and require_complete
            and (value == [] or (isinstance(value, str) and not value.strip()))
        ):
            raise ValueError(f"{key} is required")
        resolved[key] = value
    return resolved

# SPDX-License-Identifier: Apache-2.0
"""Versioned decision validation, distinct from model calibration or quality."""

from __future__ import annotations

import json
import math
import re
from importlib.resources import files
from typing import Any


def _validate(value: Any, schema: dict[str, Any], location: str) -> None:
    if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
        raise ValueError(f"{location} must equal {schema['const']!r}")
    kind = schema.get("type")
    valid = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "number": type(value) in (int, float) and math.isfinite(value),
        "integer": type(value) is int,
    }
    if kind and not valid[kind]:
        raise ValueError(f"{location} must be {kind}")
    if kind == "object":
        if set(value) != set(schema["required"]):
            raise ValueError(f"{location} contains missing or unexpected fields")
        for key, child in schema["properties"].items():
            _validate(value[key], child, f"{location}.{key}")
    if kind == "array":
        if len(value) < schema.get("minItems", 0):
            raise ValueError(f"{location} has too few items")
        if schema.get("uniqueItems") and len(
            {json.dumps(item, sort_keys=True) for item in value}
        ) != len(value):
            raise ValueError(f"{location} contains duplicate items")
        for index, item in enumerate(value):
            _validate(item, schema["items"], f"{location}[{index}]")
    if kind == "string":
        if len(value) < schema.get("minLength", 0):
            raise ValueError(f"{location} must be nonempty")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise ValueError(f"{location} has invalid format")
    if kind in {"number", "integer"}:
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise ValueError(f"{location} outside allowed range")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise ValueError(f"{location} outside allowed range")


def validate_spec(spec: Any, inventory: dict[str, Any]) -> dict[str, Any]:
    schema = json.loads(
        files("sanka_extension_llm_to_jev").joinpath("schemas/decision-v1.json").read_text()
    )
    _validate(spec, schema, "decision")
    source = spec["source"]
    matches = [item for item in inventory["call_sites"] if item["id"] == source["call_site"]]
    if len(matches) != 1 or matches[0]["status"] != "supported":
        raise ValueError("reviewed specification must select one supported call site")
    site = matches[0]
    if (source["file"], source["function"], source["sha256"]) != (
        site["file"],
        site["function"],
        site["source_hash"],
    ):
        raise ValueError("reviewed source file/function/hash mismatch")
    if (
        spec["contract"]["input"]["name"] != site["input_name"]
        or spec["state"]["input_name"] != site["input_name"]
    ):
        raise ValueError("input/state projection does not match source contract")
    labels = spec["contract"]["return"]["labels"]
    options = [option["id"] for option in spec["question"]["options"]]
    if labels != site["labels"] or options != labels:
        raise ValueError("option IDs and ordering must exactly preserve source return labels")
    if (
        spec["abstention"]["label"] != site["unknown_label"]
        or spec["fallback"]["label"] != site["unknown_label"]
    ):
        raise ValueError("abstention/fallback must preserve the existing unknown label")
    return dict(spec)

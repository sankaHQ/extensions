# SPDX-License-Identifier: Apache-2.0
"""Observation documents written by runners and their comparison."""

from __future__ import annotations

import json
from typing import Any

OBSERVATION_SCHEMA = "sanka.http-observations/v1"
OBSERVATION_KEYS = frozenset(
    {"id", "method", "path", "status", "media_type", "body", "tables", "sequences"}
)
BODYLESS = frozenset({204, 205, 304})


class ObservationError(ValueError):
    """An observation document a runner produced that violates the contract."""


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    )


def validate_observations(payload: object, scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate a runner's observation list against the scenario order."""
    if isinstance(payload, dict):
        schema = payload.get("schema", OBSERVATION_SCHEMA)
        if schema != OBSERVATION_SCHEMA:
            raise ObservationError(f"unsupported observation schema {schema!r}")
        items = payload.get("observations")
    else:
        items = payload
    if not isinstance(items, list):
        raise ObservationError("observations must be an array")
    if len(items) != len(scenarios):
        raise ObservationError(
            f"expected {len(scenarios)} observations, the runner produced {len(items)}"
        )
    observed: list[dict[str, Any]] = []
    for scenario, item in zip(scenarios, items, strict=True):
        label = f"observation {scenario['id']!r}"
        if not isinstance(item, dict):
            raise ObservationError(f"{label} must be an object")
        unknown = set(item) - OBSERVATION_KEYS
        if unknown:
            raise ObservationError(f"{label}: unknown keys " + ", ".join(sorted(unknown)))
        if item.get("id", scenario["id"]) != scenario["id"]:
            raise ObservationError(f"{label} is out of order")
        if item.get("method", scenario["method"]) != scenario["method"]:
            raise ObservationError(f"{label} reports a different method")
        if item.get("path", scenario["path"]) != scenario["path"]:
            raise ObservationError(f"{label} reports a different path")
        status = item.get("status")
        if type(status) is not int or not 100 <= status <= 599:
            raise ObservationError(f"{label}.status must be an HTTP status code")
        media_type = item.get("media_type", "")
        if not isinstance(media_type, str):
            raise ObservationError(f"{label}.media_type must be a string")
        body = item.get("body")
        try:
            canonical(body)
        except (TypeError, ValueError) as error:
            raise ObservationError(f"{label}.body must be a JSON value") from error
        record: dict[str, Any] = {
            "id": scenario["id"],
            "method": scenario["method"],
            "path": scenario["path"],
            "status": status,
            "media_type": media_type.split(";")[0].strip().lower(),
            "body": body,
        }
        for key in ("tables", "sequences"):
            if key in item:
                value = item[key]
                if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
                    raise ObservationError(f"{label}.{key} must map table names to values")
                record[key] = {name: value[name] for name in sorted(value)}
        observed.append(record)
    return observed


def difference(source: Any, candidate: Any, path: str = "$") -> str | None:
    """The JSON path and values of the first difference, or ``None`` when equal.

    Types are compared strictly: ``1``, ``1.0`` and ``true`` are three different values.
    """
    if type(source) is not type(candidate):
        return f"{path}: {_short(source)} != {_short(candidate)}"
    if isinstance(source, dict):
        for key in sorted(set(source) | set(candidate)):
            if key not in source or key not in candidate:
                missing = "source" if key not in source else "candidate"
                return f"{path}.{key}: missing on the {missing} side"
            found = difference(source[key], candidate[key], f"{path}.{key}")
            if found is not None:
                return found
        return None
    if isinstance(source, list):
        if len(source) != len(candidate):
            return f"{path}: {len(source)} items != {len(candidate)} items"
        for index, (left, right) in enumerate(zip(source, candidate, strict=True)):
            found = difference(left, right, f"{path}[{index}]")
            if found is not None:
                return found
        return None
    if canonical(source) != canonical(candidate):
        return f"{path}: {_short(source)} != {_short(candidate)}"
    return None


def _short(value: Any) -> str:
    text = canonical(value)
    return text if len(text) <= 80 else text[:77] + "..."


def compare(
    scenarios: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
    source: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compare the candidate with the expected statuses and, when given, the source.

    Media types must be ``application/json`` for every response that carries a body;
    bodyless statuses (204, 205, 304) must carry neither a body nor a media type.
    """
    steps: list[dict[str, Any]] = []
    ok = True
    sides = [candidate] if source is None else [candidate, source]
    for index, scenario in enumerate(scenarios):
        step: dict[str, Any] = {"id": scenario["id"], "status": candidate[index]["status"]}
        problems: list[str] = []
        expected = scenario.get("expected_status")
        if expected is not None and candidate[index]["status"] != expected:
            problems.append(f"status {candidate[index]['status']} != expected {expected}")
        for side, observed in zip(("candidate", "source")[: len(sides)], sides, strict=True):
            item = observed[index]
            if item["status"] in BODYLESS:
                if item["body"] is not None or item["media_type"]:
                    problems.append(f"{side}: bodyless status carries a body or media type")
            elif item["media_type"] != "application/json":
                problems.append(f"{side}: media type {item['media_type']!r} is not JSON")
        if source is not None:
            found = difference(source[index], candidate[index])
            if found is not None:
                problems.append("source != candidate at " + found)
        step["problems"] = problems
        ok = ok and not problems
        steps.append(step)
    return {"ok": ok, "steps": steps}

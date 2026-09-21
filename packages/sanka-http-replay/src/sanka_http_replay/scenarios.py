# SPDX-License-Identifier: Apache-2.0
"""The ordered scenario document and the default scenarios for captured contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCENARIO_SCHEMA = "sanka.http-scenarios/v1"
HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
MAX_SCENARIOS = 256
MAX_BODY_BYTES = 65_536
SCENARIO_KEYS = frozenset({"id", "method", "path", "headers", "body", "expected_status"})
HOSTED_ALIASES = {"expected_source_status": "expected_status"}
OPERATION_KINDS = frozenset({"literal", "list", "lookup", "create", "update", "replace", "delete"})
FIELD_TYPES = frozenset({"integer", "bigint", "boolean", "string"})
INVALID_BODY = 400
NOT_FOUND = 404
CONFLICT = 409


class ScenarioError(ValueError):
    """A scenario document that cannot be replayed; never a parity mismatch."""


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")))


def validate_scenarios(payload: object) -> list[dict[str, Any]]:
    """Validate a scenario document (an object with ``scenarios`` or a bare list)."""
    if isinstance(payload, dict):
        schema = payload.get("schema", SCENARIO_SCHEMA)
        if schema != SCENARIO_SCHEMA:
            raise ScenarioError(f"unsupported scenario schema {schema!r}")
        unknown = set(payload) - {"schema", "scenarios"}
        if unknown:
            raise ScenarioError("unknown scenario document keys: " + ", ".join(sorted(unknown)))
        items = payload.get("scenarios")
    else:
        items = payload
    if not isinstance(items, list) or not items:
        raise ScenarioError("scenarios must be a non-empty array")
    if len(items) > MAX_SCENARIOS:
        raise ScenarioError(f"at most {MAX_SCENARIOS} scenarios are replayed")
    scenarios: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        scenario = _validated(item, f"scenarios[{index}]")
        if scenario["id"] in seen:
            raise ScenarioError(f"duplicate scenario id {scenario['id']!r}")
        seen.add(scenario["id"])
        scenarios.append(scenario)
    return scenarios


def _validated(item: object, label: str) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ScenarioError(f"{label} must be an object")
    item = {HOSTED_ALIASES.get(str(key), str(key)): value for key, value in item.items()}
    unknown = set(item) - SCENARIO_KEYS
    if "setup" in unknown:
        raise ScenarioError(
            f"{label}: setup requests are not part of the ordered v1 sequence; "
            "list them as earlier scenarios"
        )
    if unknown:
        raise ScenarioError(f"{label}: unknown keys " + ", ".join(sorted(unknown)))
    identifier = item.get("id")
    if not isinstance(identifier, str) or not identifier or len(identifier) > 120:
        raise ScenarioError(f"{label}.id must be a non-empty string")
    method = item.get("method", "GET")
    if not isinstance(method, str) or method.upper() not in HTTP_METHODS:
        raise ScenarioError(f"{label}.method is not an HTTP method")
    path = item.get("path")
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or "#" in path
        or any(character.isspace() for character in path)
        or len(path) > 2048
    ):
        raise ScenarioError(f"{label}.path must be an absolute request path")
    headers = item.get("headers", {})
    if not isinstance(headers, dict) or not all(
        isinstance(key, str) and key and isinstance(value, str) for key, value in headers.items()
    ):
        raise ScenarioError(f"{label}.headers must map header names to strings")
    lowered = {key.lower(): value for key, value in headers.items()}
    if len(lowered) != len(headers):
        raise ScenarioError(f"{label}.headers repeats a header name")
    if "content-type" in lowered or "content-length" in lowered:
        raise ScenarioError(f"{label}.headers must not set content headers; use body")
    validated: dict[str, Any] = {
        "id": identifier,
        "method": method.upper(),
        "path": path,
        "headers": dict(sorted(lowered.items())),
    }
    if "body" in item:
        if method.upper() in {"GET", "HEAD", "OPTIONS"}:
            raise ScenarioError(f"{label}: {method.upper()} scenarios cannot carry a body")
        try:
            size = _json_bytes(item["body"])
        except (TypeError, ValueError) as error:
            raise ScenarioError(f"{label}.body must be a JSON value") from error
        if size > MAX_BODY_BYTES:
            raise ScenarioError(f"{label}.body exceeds {MAX_BODY_BYTES} bytes")
        validated["body"] = item["body"]
    if "expected_status" in item:
        expected = item["expected_status"]
        if type(expected) is not int or not 100 <= expected <= 599:
            raise ScenarioError(f"{label}.expected_status must be an HTTP status code")
        validated["expected_status"] = expected
    return validated


def load_scenarios(path: Path) -> list[dict[str, Any]]:
    """Read and validate a scenario file such as the hosted ``sanka-verify.json``."""
    if path.is_symlink() or not path.is_file():
        raise ScenarioError(f"{path.name} must be a regular file")
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ScenarioError(f"{path.name} exceeds the scenario document limit")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ScenarioError(f"could not read scenarios from {path.name}: {error}") from error
    return validate_scenarios(payload)


def cases_document(scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    """The runner input: the validated scenarios under the versioned schema."""
    return {"schema": SCENARIO_SCHEMA, "scenarios": validate_scenarios(scenarios)}


# ---------------------------------------------------------------------------
# Default scenarios for captured single-table contracts


def _check_models(models: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_name: dict[str, dict[str, Any]] = {}
    for model in models:
        name, table, fields = model.get("name"), model.get("table"), model.get("fields")
        if not isinstance(name, str) or not isinstance(table, str) or not isinstance(fields, list):
            raise ScenarioError("models need name, table and fields")
        primaries = 0
        for field in fields:
            if field.get("type") not in FIELD_TYPES:
                raise ScenarioError(f"model {name}: unsupported field type {field.get('type')!r}")
            for flag in ("nullable", "primary_key", "auto", "unique"):
                if type(field.get(flag, False)) is not bool:
                    raise ScenarioError(f"model {name}: {flag} must be a boolean")
            primaries += bool(field.get("primary_key"))
        if primaries != 1:
            raise ScenarioError(f"model {name}: exactly one primary key is required")
        if name in by_name:
            raise ScenarioError(f"duplicate model {name}")
        by_name[name] = model
    return by_name


def _writable(model: dict[str, Any]) -> list[dict[str, Any]]:
    return [field for field in model["fields"] if not field.get("auto", False)]


def _sample(field: dict[str, Any], variant: int) -> Any:
    kind = field["type"]
    if kind == "boolean":
        return variant % 2 == 0
    if kind in {"integer", "bigint"}:
        return (7, 9, 0)[variant % 3]
    return ("alpha", "beta", "gamma")[variant % 3] + ("" if variant < 3 else str(variant))


def _valid_body(model: dict[str, Any], variant: int, *, with_null: bool) -> dict[str, Any]:
    body: dict[str, Any] = {}
    for field in _writable(model):
        if with_null and field.get("nullable"):
            body[field["name"]] = None
        else:
            body[field["name"]] = _sample(field, variant)
    return body


def _wrong_type(field: dict[str, Any]) -> Any:
    return "seven" if field["type"] in {"integer", "bigint"} else 7


def default_scenarios(
    operations: list[dict[str, Any]], models: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Derive an ordered, deterministic scenario list from a captured contract.

    ``operations`` are ``{"method", "path", "kind", "model"?, "conflict"?}`` records
    with kinds ``literal``, ``list``, ``lookup``, ``create``, ``update``, ``replace``
    and ``delete``; ``models`` describe the captured tables with typed fields. The
    sequence assumes both databases start at the captured baseline (empty tables
    with fresh identity sequences), so the first created row has id 1.
    """
    by_name = _check_models(models)
    scenarios: list[dict[str, Any]] = []

    def add(identifier: str, method: str, path: str, status: int, body: Any = None) -> None:
        scenario: dict[str, Any] = {
            "id": identifier,
            "method": method,
            "path": path,
            "expected_status": status,
        }
        if body is not None:
            scenario["body"] = body
        scenarios.append(scenario)

    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for operation in operations:
        kind = operation.get("kind")
        if kind not in OPERATION_KINDS:
            raise ScenarioError(f"unsupported operation kind {kind!r}")
        if kind in {"literal", "list"}:
            continue
        model = operation.get("model")
        if model not in by_name:
            raise ScenarioError(f"operation {operation.get('path')} names an unknown model")
        if kind in grouped.setdefault(str(model), {}):
            raise ScenarioError(f"model {model} has more than one {kind} operation")
        grouped[str(model)][str(kind)] = operation
    for name in sorted(grouped):
        model = by_name[name]
        ops = grouped[name]
        writable = _writable(model)
        required = [field for field in writable if not field.get("nullable")]
        param_path = next(
            (
                str(ops[kind]["path"])
                for kind in ("lookup", "update", "replace", "delete")
                if kind in ops
            ),
            None,
        )
        base = param_path.rsplit("/:", 1)[0] if param_path else None
        if "create" in ops:
            path = str(ops["create"]["path"])
            conflict = bool(ops["create"].get("conflict"))
            if required:
                add(
                    f"{name}.create.missing",
                    "POST",
                    path,
                    INVALID_BODY,
                    {},
                )
                add(
                    f"{name}.create.wrong-type",
                    "POST",
                    path,
                    INVALID_BODY,
                    {
                        **_valid_body(model, 0, with_null=False),
                        required[0]["name"]: _wrong_type(required[0]),
                    },
                )
            add(
                f"{name}.create.unknown-key",
                "POST",
                path,
                INVALID_BODY,
                {**_valid_body(model, 0, with_null=False), "unexpected": 1},
            )
            add(f"{name}.create.first", "POST", path, 201, _valid_body(model, 0, with_null=True))
            if conflict and any(field.get("unique") for field in writable):
                add(
                    f"{name}.create.duplicate",
                    "POST",
                    path,
                    CONFLICT,
                    _valid_body(model, 0, with_null=True),
                )
        if base is not None:
            first, missing, invalid = f"{base}/1", f"{base}/999999", f"{base}/not-a-number"
            if "lookup" in ops:
                add(f"{name}.lookup.first", "GET", first, 200 if "create" in ops else NOT_FOUND)
                add(f"{name}.lookup.invalid-id", "GET", invalid, NOT_FOUND)
                add(f"{name}.lookup.missing", "GET", missing, NOT_FOUND)
            if "update" in ops:
                exists = "create" in ops
                partial = {writable[0]["name"]: _sample(writable[0], 1)} if writable else {}
                add(f"{name}.update.partial", "PATCH", first, 200 if exists else NOT_FOUND, partial)
                add(f"{name}.update.empty", "PATCH", first, 200 if exists else NOT_FOUND, {})
                nullable = next((field for field in writable if field.get("nullable")), None)
                if nullable is not None:
                    add(
                        f"{name}.update.clear",
                        "PATCH",
                        first,
                        200 if exists else NOT_FOUND,
                        {nullable["name"]: None},
                    )
                if required:
                    add(
                        f"{name}.update.null-required",
                        "PATCH",
                        first,
                        INVALID_BODY,
                        {required[0]["name"]: None},
                    )
                add(f"{name}.update.unknown-key", "PATCH", first, INVALID_BODY, {"unexpected": 1})
                add(f"{name}.update.missing", "PATCH", missing, NOT_FOUND, partial)
            if "replace" in ops:
                exists = "create" in ops
                if required:
                    add(
                        f"{name}.replace.missing-field",
                        "PUT",
                        first,
                        INVALID_BODY,
                        {},
                    )
                add(
                    f"{name}.replace.first",
                    "PUT",
                    first,
                    200 if exists else NOT_FOUND,
                    _valid_body(model, 2, with_null=False),
                )
                add(
                    f"{name}.replace.missing",
                    "PUT",
                    missing,
                    NOT_FOUND,
                    _valid_body(model, 2, with_null=False),
                )
            if "delete" in ops:
                exists = "create" in ops
                add(f"{name}.delete.first", "DELETE", first, 204 if exists else NOT_FOUND)
                add(f"{name}.delete.again", "DELETE", first, NOT_FOUND)
                add(f"{name}.delete.invalid-id", "DELETE", invalid, NOT_FOUND)
        if "create" in ops:
            add(
                f"{name}.create.after",
                "POST",
                str(ops["create"]["path"]),
                201,
                _valid_body(model, 3, with_null=False),
            )
    for operation in operations:
        if operation.get("kind") in {"literal", "list"}:
            path = str(operation["path"])
            add(
                f"get{path.replace('/', '.')}",
                str(operation.get("method", "GET")),
                path,
                int(operation.get("status", 200)),
            )
    return validate_scenarios(scenarios)

# SPDX-License-Identifier: Apache-2.0
"""Reviewed HTTP scope and ownership receipts shared by backend generators."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

SCHEMA = "sanka.endpoint-scope/v1"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def endpoint_id(route: dict[str, Any]) -> str:
    path = "/" + (route.get("source_path") or route["path"]).lstrip("/")
    return f"{route['method']} {path}"


def _safe(path: Path) -> Path:
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError(f"Endpoint ownership paths cannot be symlinks: {path}")
    return path


def _file(output: Path, name: str) -> Path:
    if not isinstance(name, str):
        raise ValueError("Invalid generated file in endpoint receipt")
    relative = Path(name)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or "\\" in name
        or not relative.parts
        or name != relative.as_posix()
    ):
        raise ValueError("Invalid generated file in endpoint receipt")
    return _safe(output / relative)


def _receipt_path(artifacts: Path, output: Path) -> Path:
    return _safe(artifacts / "endpoint-receipts" / (digest(str(output)) + ".json"))


def receipt(artifacts: Path, output: Path) -> dict[str, Any] | None:
    path = _receipt_path(artifacts, output)
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    if (
        not isinstance(value, dict)
        or value.get("schema") != SCHEMA
        or value.get("output") != str(output)
        or not isinstance(value.get("files"), dict)
        or not value["files"]
        or not isinstance(value.get("routes"), dict)
        or not all(
            isinstance(value.get(key), str) and value[key]
            for key in ("target", "context", "plan_hash")
        )
        or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in value["routes"].items()
        )
    ):
        raise ValueError("Invalid endpoint apply receipt; preserve the candidate")
    for name, expected in value["files"].items():
        file = _file(output, name)
        if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Generated file changed or missing: {name}; preserve repairs")
    return value


def plan_scope(
    routes: list[dict[str, Any]],
    requested: Any,
    *,
    artifacts: Path,
    output: Path,
    target: str,
    context: Any,
) -> dict[str, Any]:
    _safe(output)
    identities = [endpoint_id(route) for route in routes]
    ambiguous = len(set(identities)) != len(identities)
    if ambiguous and requested is not None:
        raise ValueError("Ambiguous endpoint identities; partial migration is unavailable")
    if requested is not None and (
        not isinstance(requested, list)
        or not requested
        or any(not isinstance(item, str) for item in requested)
        or len(set(requested)) != len(requested)
        or set(requested) - set(identities)
    ):
        raise ValueError("selected_endpoints must be a nonempty array of unique captured IDs")
    selected = sorted(set(identities) if requested is None else requested)
    previous = receipt(artifacts, output)
    hashes = {endpoint_id(route): digest(route) for route in routes}
    retained = sorted(previous["routes"]) if previous else []
    if previous:
        if previous["target"] != target or previous["context"] != digest(context):
            raise ValueError("Generated target configuration or shared dependencies changed")
        if any(hashes.get(key) != value for key, value in previous["routes"].items()):
            raise ValueError("A generated endpoint changed in the source; reconcile it first")
        if set(retained) - set(selected):
            raise ValueError("Previously generated endpoints must remain selected")
    return {
        "schema": SCHEMA,
        "output": os.path.relpath(output, artifacts),
        "target": target,
        "context": digest(context),
        "effective_ids": selected,
        "omitted_ids": sorted(set(identities) - set(selected)),
        "retained_ids": retained,
        "routes": {key: hashes[key] for key in selected},
        "receipt_digest": digest(previous) if previous else "",
        "dependencies": ["Required shared authentication, models and schema are retained."],
        "endpoints": [
            {
                "id": key,
                "method": route["method"],
                "path": key.split(" ", 1)[1],
                "selected": key in selected,
                "locked": key in retained,
                "status": "generated" if key in retained else "new",
            }
            for key, route in ([] if ambiguous else zip(identities, routes, strict=True))
        ],
    }


def generated_scope(scope: dict[str, Any], generated: list[str], *, explicit: bool) -> None:
    """Manual gaps are visible, but cannot become successful apply receipts."""
    scope["generated_ids"] = sorted(set(generated))
    missing = set(scope["effective_ids"]) - set(generated)
    if explicit and missing:
        raise ValueError(
            "Selected endpoints require manual adaptation: " + ", ".join(sorted(missing))
        )
    for row in scope["endpoints"]:
        if row["id"] in missing:
            row.update(blocked=True, selected=False, status="manual")


def scoped_routes(routes: list[dict[str, Any]], scope: dict[str, Any]) -> list[dict[str, Any]]:
    selected = set(scope["effective_ids"])
    result = []
    for route in routes:
        if endpoint_id(route) not in selected:
            continue
        route = dict(route)
        if "allow" in route:
            path = endpoint_id(route).split(" ", 1)[1]
            omitted = {
                key.split(" ", 1)[0] for key in scope["omitted_ids"] if key.split(" ", 1)[1] == path
            }
            if "GET" in omitted:
                omitted.add("HEAD")
            route["allow"] = ", ".join(
                method.strip()
                for method in route["allow"].split(",")
                if method.strip() not in omitted
            )
        result.append(route)
    return result


def check_selection(requested: Any, scope: dict[str, Any]) -> None:
    if requested is not None and (
        not isinstance(requested, list)
        or any(not isinstance(item, str) for item in requested)
        or sorted(requested) != scope["effective_ids"]
    ):
        raise ValueError("Endpoint selection differs from the reviewed plan")


def _output(artifacts: Path, scope: dict[str, Any]) -> Path:
    return _safe(_safe(artifacts / scope["output"]).resolve())


def check_apply(
    artifacts: Path, scope: dict[str, Any], plan_hash: str | None = None
) -> dict[str, Any] | None:
    previous = receipt(artifacts, _output(artifacts, scope))
    if previous and plan_hash and previous["plan_hash"] == plan_hash:
        return previous  # An identical reviewed apply is safe only while every owned file matches.
    if (digest(previous) if previous else "") != scope["receipt_digest"]:
        raise ValueError("Endpoint apply receipt changed after planning; review a new plan")
    return previous


def record_apply(
    artifacts: Path,
    scope: dict[str, Any],
    names: list[str],
    plan_hash: str,
) -> None:
    output = _output(artifacts, scope)
    value = {
        "schema": SCHEMA,
        "output": str(output),
        "target": scope["target"],
        "context": scope["context"],
        "routes": {
            key: scope["routes"][key] for key in scope.get("generated_ids", scope["effective_ids"])
        },
        "plan_hash": plan_hash,
        "files": {
            name: hashlib.sha256(_file(output, name).read_bytes()).hexdigest() for name in names
        },
    }
    path = _receipt_path(artifacts, output)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    _safe(temporary).write_text(json.dumps(value, sort_keys=True) + "\n")
    os.replace(temporary, path)


def apply_files(
    artifacts: Path,
    scope: dict[str, Any],
    files: dict[str, str],
    plan_hash: str,
) -> None:
    """Stage a cumulative candidate; retain unrelated files and recover on rename failure."""
    output = _output(artifacts, scope)
    previous = check_apply(artifacts, scope)
    if output.exists() and previous is None:
        raise ValueError("Output already exists without an ownership receipt; preserve repairs")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".endpoint-stage-", dir=output.parent))
    backup = output.with_name(output.name + ".sanka-backup")
    if backup.exists() or backup.is_symlink():
        shutil.rmtree(staging)
        raise ValueError(f"An interrupted update requires recovery from {backup}")
    try:
        if previous:
            shutil.copytree(output, staging, dirs_exist_ok=True, symlinks=True)
            for name in files:
                destination = _file(output, name)
                if destination.exists() and name not in previous["files"]:
                    raise ValueError(f"New generated file would replace an unowned file: {name}")
            for name in previous["files"]:
                _file(staging, name).unlink()
        for name, content in files.items():
            destination = _file(staging, name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content)
        check_apply(artifacts, scope)
        if previous is None and output.exists():
            raise ValueError("Output appeared after planning; preserve it and plan again")
        if previous:
            output.rename(backup)
        try:
            staging.rename(output)
            record_apply(artifacts, scope, sorted(files), plan_hash)
        except BaseException:
            if previous:
                if output.exists():
                    output.rename(staging)
                backup.rename(output)
            raise
        if previous:
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)

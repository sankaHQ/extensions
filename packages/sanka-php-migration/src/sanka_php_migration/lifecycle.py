# SPDX-License-Identifier: Apache-2.0
"""The same review-bound lifecycle serves both independently packaged targets."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from sanka_code_migration.endpoints import apply_files, check_selection, plan_scope, scoped_routes
from sanka_code_migration.ir import OperationFact, SourceSetting, VersionPin
from sanka_code_migration.ir_v2 import BackendIRV2, EffectiveInputsV2, TargetProfileV2

from sanka_extensions.code import (
    ExtensionRequest,
    ExtensionResponse,
    failure_response,
    success_response,
)

from .capture import VERSION, canonical, capture, digest
from .render import render

LIMITATION = "Selected PHP API closure only; no whole-application or production-cutover claim."


def safe(root: Path, path: Path) -> Path:
    if path != path.resolve() or not path.is_relative_to(root / ".sanka") or path == root:
        raise ValueError("artifacts must be canonical paths inside the project's .sanka directory")
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("artifact paths must not contain symlinks")
    return path


def write_json(root: Path, path: Path, value: Any) -> None:
    safe(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = safe(root, path.with_suffix(".tmp"))
    temporary.write_text(canonical(value) + "\n")
    os.replace(temporary, path)


def inputs(captured: dict[str, Any], target: str) -> dict[str, object]:
    dialect = captured["database"]["dialect"]
    persistence = dialect != "none"
    source = BackendIRV2(
        source_content_digest="sha256:" + captured["source_digest"],
        source_dependency_lock_digest="sha256:"
        + captured["source_inventory"].get("composer.lock", digest({})),
        source_settings=(SourceSetting.from_value("source_kind", captured["kind"]),),
        persistence=persistence,
        database_dialect=dialect,
        route_groups=tuple(sorted({route["path"] for route in captured["routes"]})),
        operations=tuple(
            OperationFact(
                route["method"] + " " + route["path"],
                needs_persistence_boundary=persistence,
                evidence=(f"{route['file']}:{route['line']}",),
            )
            for route in captured["routes"]
        ),
    )
    native = "sqlalchemy" if target == "fastapi" else "database-sql"
    profile = TargetProfileV2(
        profile_version="php-route-closures-v1",
        target=target,
        orm=native if persistence else "none",
        dialect=dialect,
        schema_mode="adopt-existing" if persistence else "none",
        layout="modular",
        services=(),
        repositories=tuple(captured["database"].get("tables", [])),
        dependency_locks=(VersionPin("fastapi", "0.141.1"), VersionPin("sqlalchemy", "2.0.52"))
        if target == "fastapi"
        else (VersionPin("chi", "5.5.0"),),
        toolchain_locks=(
            VersionPin("php", "8.4.27"),
            VersionPin("tree-sitter-php", "0.24.1"),
            VersionPin("python", "3.12.13") if target == "fastapi" else VersionPin("go", "1.27.0"),
        ),
        locks_complete=True,
        reasons=(),
    )
    return EffectiveInputsV2(source, profile, VERSION).to_dict()


def handle(request: ExtensionRequest, target: str) -> ExtensionResponse:
    try:
        extension = "sanka/php-to-" + ("fastapi" if target == "fastapi" else "golang")
        if (
            target not in {"fastapi", "chi"}
            or request.extension_id != extension
            or request.extension_version != VERSION
        ):
            raise ValueError("extension identity or version mismatch")
        root = Path(request.project_root)
        if root != root.resolve() or not root.is_dir():
            raise ValueError("project_root must be a canonical directory")
        artifacts = safe(root, Path(request.artifact_root))
        if request.command in {"test", "verify"}:
            safe(root, artifacts / f"{request.command}.json").unlink(missing_ok=True)
        allowed = {
            "target_framework",
            "database_dialect",
            "selected_endpoints",
            "extension_plan_hash",
        }
        if set(request.configuration) - allowed:
            raise ValueError("unsupported PHP recipe configuration")
        if request.configuration.get("target_framework", target) != target:
            raise ValueError("extension target cannot be changed")
        captured = capture(root)
        dialect = captured["database"]["dialect"]
        if request.configuration.get("database_dialect", dialect) != dialect:
            raise ValueError("the source database engine must be preserved")
        if request.command == "scan":
            destination = artifacts / "scan.json"
            write_json(root, destination, captured)
            return success_response(
                request, data=captured, artifacts=[str(destination)], limitations=[LIMITATION]
            )
        if request.command not in {"plan", "apply", "test", "verify"}:
            return failure_response(
                request,
                code="SANKA_EXTENSION_UNSUPPORTED_COMMAND",
                message=f"Unsupported command: {request.command}",
            )
        plan_file = safe(root, artifacts / "plan.json")
        output = safe(root, artifacts / target)
        selected = request.configuration.get("selected_endpoints")
        if request.command == "plan":
            scope = plan_scope(
                captured["routes"],
                selected,
                artifacts=artifacts,
                output=output,
                target=target,
                context={
                    "kind": captured["kind"],
                    "database": captured["database"],
                    "composer": captured["composer"],
                    "version": VERSION,
                },
            )
        else:
            scope = json.loads(plan_file.read_text())["endpoint_scope"]
            if scope.get("output") != target or scope.get("target") != target:
                raise ValueError("saved endpoint scope names another output")
        check_selection(selected, scope)
        captured["routes"] = scoped_routes(captured["routes"], scope)
        if not captured["routes"]:
            raise ValueError("no selected PHP routes")
        blockers = list(captured["gaps"])
        for route in captured["routes"]:
            blockers.extend(f"{route['method']} {route['path']}: {gap}" for gap in route["gaps"])
        generated = {} if blockers else render(captured, target)
        data: dict[str, Any] = {
            "schema": "sanka.php-plan/v1",
            "extension_id": extension,
            "extension_version": VERSION,
            "manifest_digest": request.manifest_digest,
            "capture": captured,
            "inputs": inputs(captured, target),
            "endpoint_scope": scope,
            "files": generated,
            "blockers": blockers,
        }
        plan_hash = "sha256:" + digest(data)
        data["plan_hash"] = plan_hash
        if request.command == "plan":
            write_json(root, plan_file, data)
            return success_response(
                request, data=data, artifacts=[str(plan_file)], limitations=[LIMITATION]
            )
        if json.loads(plan_file.read_text()) != data:
            raise ValueError("source, configuration or extension differs from the saved plan")
        if blockers:
            raise ValueError("selected PHP behavior requires adaptation; inspect plan blockers")
        if request.command == "apply":
            if (
                not request.reviewed_plan_hash
                or request.configuration.get("extension_plan_hash") != plan_hash
            ):
                raise ValueError("extension_plan_hash must match the reviewed current plan")
            apply_files(artifacts, scope, generated, plan_hash)
            return success_response(
                request,
                data={"output": str(output), "plan_hash": plan_hash, "complete_backend": False},
                artifacts=[str(output)],
                limitations=[LIMITATION],
            )
        from .replay import replay

        report = replay(root, output, captured, target, request.command)
        report["plan_hash"] = plan_hash
        destination = artifacts / f"{request.command}.json"
        write_json(root, destination, report)
        if not report["ok"]:
            return failure_response(
                request,
                code="SANKA_EXTENSION_PARITY_FAILED",
                message=f"PHP behavioral checks failed; inspect {request.command}.json",
                details={"report": str(destination)},
            )
        return success_response(
            request, data=report, artifacts=[str(destination)], limitations=[LIMITATION]
        )
    except (ValueError, OSError, KeyError, TypeError) as error:
        return failure_response(
            request, code="SANKA_EXTENSION_EXECUTION_FAILED", message=str(error)
        )

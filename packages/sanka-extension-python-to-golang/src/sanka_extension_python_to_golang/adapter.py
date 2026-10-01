# SPDX-License-Identifier: Apache-2.0
"""Review-bound planning; unsupported behavior never becomes generated placeholders."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from sanka_code_migration.endpoints import apply_files, check_selection, plan_scope, scoped_routes

from sanka_extensions.code import (
    ExtensionRequest,
    ExtensionResponse,
    failure_response,
    success_response,
)

from .capture import VERSION, _python_path, canonical, capture, configuration, digest
from .render import render
from .replay import replay


def _safe(root: Path, path: Path) -> Path:
    if not path.is_relative_to(root) or path == root:
        raise ValueError("artifacts must be inside the project")
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("artifact paths must not contain symlinks")
    return path


def handle(request: ExtensionRequest) -> ExtensionResponse:
    try:
        if request.extension_id != "sanka/python-to-golang" or request.extension_version != VERSION:
            raise ValueError("extension identity or version mismatch")
        root = Path(request.project_root)
        if root != root.resolve() or not root.is_dir():
            raise ValueError("project_root must be a canonical directory")
        artifacts = _safe(root, Path(request.artifact_root))
        if not artifacts.is_relative_to(root / ".sanka"):
            raise ValueError("artifact_root must be under the project's .sanka directory")
        if request.command in {"test", "verify"}:
            # Never leave a previous passing report after a failed rerun.
            _safe(root, artifacts / f"{request.command}.json").unlink(missing_ok=True)
        values = {k: v for k, v in request.configuration.items() if k != "selected_endpoints"}
        if values.get("source_file") in (None, ""):
            entrypoints = (
                [root / "app.py"] if (root / "app.py").is_file() else sorted(root.glob("*/urls.py"))
            )
            if len(entrypoints) != 1:
                return failure_response(
                    request,
                    code="SANKA_EXTENSION_INPUT_REQUIRED",
                    message="Choose the Python entrypoint relative to this project",
                    details={"inputs": ["source_file"]},
                )
            values["source_file"] = entrypoints[0].relative_to(root).as_posix()
        if values.get("source_framework", "auto") == "auto":
            source_file = values["source_file"]
            if not isinstance(source_file, str):
                raise ValueError("source_file must be a relative Python path")
            filename = _python_path(source_file, "source_file")
            path = root / filename
            if (
                path.is_symlink()
                or not path.resolve().is_relative_to(root)
                or path.stat().st_size > 1024 * 1024
            ):
                raise ValueError("source_file must be a bounded regular project file")
            tree = ast.parse(path.read_text())
            imports = {
                node.module.split(".")[0]
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            }
            imports |= {
                alias.name.split(".")[0]
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            }
            frameworks = imports & {"flask", "fastapi", "rest_framework"}
            if not frameworks and "django" in imports and filename.endswith("/urls.py"):
                frameworks = {"rest_framework"}
            if len(frameworks) != 1:
                return failure_response(
                    request,
                    code="SANKA_EXTENSION_INPUT_REQUIRED",
                    message="Choose drf, fastapi or flask; the entrypoint is ambiguous",
                    details={"inputs": ["source_framework"]},
                )
            values["source_framework"] = {"rest_framework": "drf"}.get(
                next(iter(frameworks)), next(iter(frameworks))
            )
        config = configuration(values)
        captured = capture(root, config)
        if request.command == "scan":
            data = captured
            name = "scan.json"
        elif request.command in {"plan", "apply", "test", "verify"}:
            scope = (
                plan_scope(
                    captured["routes"],
                    request.configuration.get("selected_endpoints"),
                    artifacts=artifacts,
                    output=_safe(root, artifacts / "golang"),
                    target=config["target_framework"],
                    context={
                        k: v
                        for k, v in captured.items()
                        if k not in {"routes", "source_digest", "source_inventory", "gaps"}
                    },
                )
                if request.command == "plan"
                else json.loads(_safe(root, artifacts / "plan.json").read_text())["endpoint_scope"]
            )
            requested = request.configuration.get("selected_endpoints")
            check_selection(requested, scope)
            captured["routes"] = scoped_routes(captured["routes"], scope)
            generated = render(captured) if not captured["gaps"] else {}
            data = {
                "schema": "sanka.python-to-golang.plan/v1",
                "extension_version": VERSION,
                "capture": captured,
                "files": generated,
                "endpoint_scope": scope,
            }
            plan_hash = digest(data)
            data["plan_hash"] = plan_hash
            name = "plan.json"
            if request.command in {"test", "verify"}:
                saved = _safe(root, artifacts / name)
                if not saved.is_file() or json.loads(saved.read_text()) != data:
                    raise ValueError("source or configuration differs from the applied plan")
                output = _safe(root, artifacts / "golang")
                report = replay(root, output, captured, request.command)
                report["plan_hash"] = plan_hash
                report["qualification"] = {
                    "candidate_executed": True,
                    "source_compared": "source" in report,
                    "original_tests_executed": bool(
                        report.get("original_tests", {}).get("tests_run")
                    ),
                    "cutover_qualified": False,
                }
                destination = _safe(root, artifacts / f"{request.command}.json")
                destination.write_text(canonical(report) + "\n")
                if not report["ok"]:
                    return failure_response(
                        request,
                        code="SANKA_EXTENSION_PARITY_FAILED",
                        message=f"Verification checks failed; inspect {request.command}.json",
                        details={"report": str(destination)},
                    )
                return success_response(
                    request,
                    data=report,
                    artifacts=[str(destination)],
                    limitations=[
                        "Only captured endpoint scenarios and selected original source tests "
                        "were exercised; not cutover readiness."
                        if report.get("original_tests")
                        else "Only captured endpoint scenarios were exercised; "
                        "original source tests were not run; not cutover readiness."
                    ],
                )
            if request.command == "apply":
                if captured["gaps"]:
                    raise ValueError("capture has unsupported behavior; inspect plan gaps")
                reviewed = request.configuration.get("extension_plan_hash")
                if not request.reviewed_plan_hash or not reviewed or reviewed != plan_hash:
                    raise ValueError("extension_plan_hash must match the reviewed current plan")
                saved = artifacts / name
                _safe(root, saved)
                if not saved.is_file() or json.loads(saved.read_text()) != data:
                    raise ValueError("saved plan differs; plan and review again")
                output = _safe(root, artifacts / "golang")
                apply_files(artifacts, scope, generated, plan_hash)
                return success_response(
                    request,
                    data={"output": str(output), "plan_hash": plan_hash, "complete_backend": False},
                    artifacts=[str(output)],
                    limitations=["Experimental endpoint contract only; no cutover qualification."],
                )
        else:
            return failure_response(
                request,
                code="SANKA_EXTENSION_UNSUPPORTED_COMMAND",
                message=f"Unsupported command: {request.command}",
            )
        artifacts.mkdir(parents=True, exist_ok=True)
        destination = _safe(root, artifacts / name)
        destination.write_text(canonical(data) + "\n")
        return success_response(
            request,
            data=data,
            artifacts=[str(destination)],
            limitations=["Experimental endpoint contract only; not a complete backend migration."],
        )
    except (ValueError, OSError, SyntaxError, KeyError, TypeError) as error:
        return failure_response(
            request, code="SANKA_EXTENSION_EXECUTION_FAILED", message=str(error)
        )

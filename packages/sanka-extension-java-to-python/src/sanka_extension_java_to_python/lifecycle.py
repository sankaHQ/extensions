# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from sanka_java_migration import capture as java_capture
from sanka_java_migration.capture import VERSION, canonical, capture, digest

from sanka_extensions.code import (
    ExtensionRequest,
    ExtensionResponse,
    failure_response,
    success_response,
)
from sanka_http_replay import load_scenarios

from .render import render

LIMITATION = (
    "Qualified stateless GET or flat PostgreSQL JPA CRUD profile; recorded cases only. "
    "No auth, relationships, custom queries, concurrent writes, HEAD/OPTIONS, arbitrary headers, "
    "live transport or production-cutover claim."
)


def implementation_digest() -> str:
    files = {}
    for base in (Path(java_capture.__file__).parent, Path(__file__).parent):
        for path in sorted(base.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                files[base.name + "/" + path.relative_to(base).as_posix()] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
    return digest(files)


def safe(root: Path, path: Path) -> Path:
    if (
        path != path.resolve()
        or not path.is_relative_to(root / ".sanka")
        or any(p.is_symlink() for p in (path, *path.parents))
    ):
        raise ValueError("artifacts must be canonical paths within the project's .sanka directory")
    return path


def write(root: Path, path: Path, value: Any) -> None:
    safe(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = safe(root, path.with_suffix(".tmp"))
    temporary.write_text(canonical(value) + "\n")
    os.replace(temporary, path)


def cases(root: Path, captured: dict[str, Any]) -> list[dict[str, Any]]:
    scenarios = load_scenarios(root / "sanka-verify.json")
    covered: set[str] = set()
    crud = "persistence" in captured
    for case in scenarios:
        url = urlsplit(case["path"])
        if (
            (not crud and (case["method"] != "GET" or case.get("expected_status") != 200))
            or (crud and case.get("expected_status") not in {200, 201, 204, 400, 404, 409})
            or case["headers"]
            or url.netloc
            or url.scheme
        ):
            raise ValueError("scenarios require a qualified method/status and no custom headers")
        matches = [
            e["method"] + " " + e["path"]
            for e in captured["endpoints"]
            if e["method"] == case["method"]
            and re.fullmatch(
                re.sub(r"\\\{[^}]+\\\}", "[^/;]+", re.escape(e["path"])), unquote(url.path)
            )
        ]
        if not matches:
            raise ValueError(f"scenario {case['id']} does not exercise a captured route")
        covered.add(min(matches, key=lambda path: path.count("{")))
    if {e["method"] + " " + e["path"] for e in captured["endpoints"]} - covered:
        raise ValueError("every captured route needs at least one scenario")
    if crud:
        observed_statuses = {c["expected_status"] for c in scenarios}
        if not {200, 201, 204, 400, 404, 409} <= observed_statuses:
            raise ValueError(
                "CRUD scenarios require create/read/update/delete, "
                "invalid input, missing and rollback cases"
            )
    return scenarios


def handle(request: ExtensionRequest) -> ExtensionResponse:
    try:
        if request.extension_id != "sanka/java-to-python" or request.extension_version != VERSION:
            raise ValueError("extension identity/version mismatch")
        root = Path(request.project_root)
        if root != root.resolve() or not root.is_dir():
            raise ValueError("project_root must be a canonical directory")
        artifacts = safe(root, Path(request.artifact_root))
        if request.command in {"test", "verify"}:
            safe(root, artifacts / f"{request.command}.json").unlink(missing_ok=True)
        if (
            set(request.configuration) - {"target", "extension_plan_hash"}
            or request.configuration.get("target", "python-fastapi") != "python-fastapi"
        ):
            raise ValueError(
                "only the python-fastapi target is supported; endpoint selection is deferred"
            )
        captured = capture(root)
        if request.command == "scan":
            destination = artifacts / "scan.json"
            write(root, destination, captured)
            return success_response(
                request, data=captured, artifacts=[str(destination)], limitations=[LIMITATION]
            )
        if request.command not in {"plan", "apply", "test", "verify"}:
            raise ValueError("unsupported lifecycle command")
        blockers = list(captured["gaps"]) + [
            f"{e['file']}:{e['line']}: {gap}" for e in captured["endpoints"] for gap in e["gaps"]
        ]
        scenarios = []
        if not blockers:
            try:
                scenarios = cases(root, captured)
            except ValueError as error:
                blockers.append(str(error))
        generated = render(captured, scenarios) if not blockers else {}
        data: dict[str, Any] = {
            "schema": "sanka.java-plan/v1",
            "extension_id": request.extension_id,
            "extension_version": VERSION,
            "manifest_digest": request.manifest_digest,
            "implementation_digest": implementation_digest(),
            "target": "python-fastapi",
            "capture": captured,
            "scenarios": scenarios,
            "files": generated,
            "blockers": blockers,
        }
        plan_hash = "sha256:" + digest(data)
        data["plan_hash"] = plan_hash
        plan_file = safe(root, artifacts / "plan.json")
        output = safe(root, artifacts / "python-fastapi")
        if request.command == "plan":
            write(root, plan_file, data)
            return success_response(
                request, data=data, artifacts=[str(plan_file)], limitations=[LIMITATION]
            )
        if json.loads(plan_file.read_text()) != data:
            raise ValueError(
                "source, scenarios, configuration or converter differs from the saved plan"
            )
        if blockers:
            raise ValueError("plan has blockers; inspect plan.json")
        if request.command == "apply":
            if (
                not request.reviewed_plan_hash
                or request.configuration.get("extension_plan_hash") != plan_hash
            ):
                raise ValueError(
                    "reviewed runtime hash and matching extension_plan_hash are required"
                )
            if output.exists():
                raise ValueError("candidate output already exists")
            staging = Path(tempfile.mkdtemp(prefix=".java-candidate-", dir=artifacts))
            try:
                for name, content in generated.items():
                    (staging / name).write_text(content)
                staging.rename(output)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
            return success_response(
                request,
                data={"output": str(output), "plan_hash": plan_hash, "complete_backend": False},
                artifacts=[str(output)],
                limitations=[LIMITATION],
            )
        from .replay import replay

        report = replay(root, output, captured, scenarios, request.command)
        report["plan_hash"] = plan_hash
        destination = artifacts / f"{request.command}.json"
        write(root, destination, report)
        if not report["ok"]:
            return failure_response(
                request,
                code="SANKA_EXTENSION_PARITY_FAILED",
                message="Java behavioral comparison failed",
                details={"report": str(destination)},
            )
        return success_response(
            request, data=report, artifacts=[str(destination)], limitations=[LIMITATION]
        )
    except (ValueError, OSError, KeyError, TypeError) as error:
        return failure_response(
            request, code="SANKA_EXTENSION_EXECUTION_FAILED", message=str(error)
        )

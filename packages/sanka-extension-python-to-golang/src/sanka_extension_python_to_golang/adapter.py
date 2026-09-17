# SPDX-License-Identifier: Apache-2.0
"""Review-bound planning; unsupported behavior never becomes generated placeholders."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from sanka_extensions.code import (
    ExtensionRequest,
    ExtensionResponse,
    failure_response,
    success_response,
)

from .capture import VERSION, canonical, capture, configuration, digest
from .render import render


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
        config = configuration(request.configuration)
        captured = capture(root, config)
        if request.command == "scan":
            data = captured
            name = "scan.json"
        elif request.command in {"plan", "apply"}:
            generated = render(captured) if not captured["gaps"] else {}
            data = {
                "schema": "sanka.python-to-golang.plan/v1",
                "extension_version": VERSION,
                "capture": captured,
                "files": generated,
            }
            plan_hash = digest(data)
            data["plan_hash"] = plan_hash
            name = "plan.json"
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
                if output.exists():
                    raise ValueError("output already exists; preserve repairs")
                staging = Path(tempfile.mkdtemp(prefix=".golang-", dir=artifacts))
                try:
                    for filename, content in generated.items():
                        (staging / filename).write_text(content)
                    # mkdir reserves the destination without replacing concurrent work.
                    output.mkdir()
                    for source in staging.iterdir():
                        os.rename(source, output / source.name)
                finally:
                    shutil.rmtree(staging)
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
                message="Runtime test/verify is not qualified; use the integration suite.",
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
    except (ValueError, OSError, SyntaxError) as error:
        return failure_response(
            request, code="SANKA_EXTENSION_EXECUTION_FAILED", message=str(error)
        )

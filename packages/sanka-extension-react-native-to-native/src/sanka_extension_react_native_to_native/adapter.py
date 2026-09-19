# SPDX-License-Identifier: Apache-2.0
"""Review-bound scan, plan, apply, test and verify for the SwiftUI target."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from sanka_extensions.code import (
    ExtensionRequest,
    ExtensionResponse,
    failure_response,
    success_response,
)

from .capture import VERSION, canonical, capture, configuration, digest
from .render_swiftui import OUTPUT, Rendered, render_swiftui
from .replay import replay

EXTENSION_ID = "sanka/react-native-to-native"
PLAN_SCHEMA = "sanka.react-native-to-native.plan/v1"
LIMITATIONS = [
    "Experimental slice-1 screens only (navigation, static trees, literal styles, local "
    "state); not a complete app migration.",
    "Structural parity compares normalized screen trees, never layout, colors or pixels.",
]
COMPOSE_MESSAGE = (
    "Jetpack Compose generation, tests and verification arrive with the Compose emitter; "
    "use target_framework swiftui"
)


def _safe(root: Path, path: Path) -> Path:
    if not path.is_relative_to(root) or path == root:
        raise ValueError("artifacts must be inside the project")
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("artifact paths must not contain symlinks")
    return path


def _dispositions(captured: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "module": screen["module"],
            "disposition": screen["disposition"],
            "adaptation_reasons": list(screen["adaptation_reasons"]),
        }
        for screen in sorted(captured["screens"], key=lambda item: str(item["module"]))
    ]


def _plan(captured: dict[str, Any]) -> tuple[dict[str, Any], Rendered]:
    target = captured["configuration"]["target_framework"]
    if target == "swiftui" and not captured["gaps"]:
        rendered = render_swiftui(captured)
    else:
        rendered = Rendered({}, {}, _dispositions(captured), [])
    native = sum(item["disposition"] == "native-screen" for item in rendered.dispositions)
    data: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "extension_version": VERSION,
        "capture": captured,
        "files": rendered.files,
        "assets": rendered.assets,
        "generated": bool(rendered.files),
        "gaps": rendered.gaps,
        "readiness": native / len(rendered.dispositions) if rendered.dispositions else 0.0,
        "dispositions": rendered.dispositions,
    }
    data["plan_hash"] = digest(data)
    return data, rendered


def handle(request: ExtensionRequest) -> ExtensionResponse:
    try:
        if request.extension_id != EXTENSION_ID or request.extension_version != VERSION:
            raise ValueError("extension identity or version mismatch")
        root = Path(request.project_root)
        if root != root.resolve() or not root.is_dir():
            raise ValueError("project_root must be a canonical directory")
        artifacts = _safe(root, Path(request.artifact_root))
        if not artifacts.is_relative_to(root / ".sanka"):
            raise ValueError("artifact_root must be under the project's .sanka directory")
        if request.command not in {"scan", "plan", "apply", "test", "verify"}:
            return failure_response(
                request,
                code="SANKA_EXTENSION_UNSUPPORTED_COMMAND",
                message=f"Unsupported command: {request.command}",
            )
        if request.command in {"test", "verify"}:
            # Never leave a previous passing report after a failed rerun.
            _safe(root, artifacts / f"{request.command}.json").unlink(missing_ok=True)
        config = configuration(request.configuration)
        if (
            request.command in {"apply", "test", "verify"}
            and config["target_framework"] != "swiftui"
        ):
            return failure_response(
                request,
                code="SANKA_EXTENSION_UNSUPPORTED_COMMAND",
                message=f"Unsupported command: {request.command}; {COMPOSE_MESSAGE}",
            )
        captured = capture(root, config)
        if request.command == "scan":
            data: dict[str, Any] = captured
            name = "scan.json"
        else:
            data, rendered = _plan(captured)
            name = "plan.json"
            if request.command in {"test", "verify"}:
                saved = _safe(root, artifacts / name)
                if not saved.is_file() or json.loads(saved.read_text()) != data:
                    raise ValueError("source or configuration differs from the applied plan")
                output = _safe(root, artifacts / OUTPUT)
                report = replay(root, output, captured, rendered, request.command)
                report["plan_hash"] = data["plan_hash"]
                destination = _safe(root, artifacts / f"{request.command}.json")
                destination.write_text(canonical(report) + "\n")
                if not report["ok"]:
                    return failure_response(
                        request,
                        code="SANKA_EXTENSION_PARITY_FAILED",
                        message=(
                            f"structural parity checks failed; inspect {request.command}.json"
                        ),
                        details={"report": str(destination)},
                    )
                return success_response(
                    request,
                    data=report,
                    artifacts=[str(destination)],
                    limitations=LIMITATIONS,
                )
            if request.command == "apply":
                return _apply(request, root, artifacts, data, rendered)
        artifacts.mkdir(parents=True, exist_ok=True)
        destination = _safe(root, artifacts / name)
        destination.write_text(canonical(data) + "\n")
        return success_response(
            request,
            data=data,
            artifacts=[str(destination)],
            limitations=LIMITATIONS,
        )
    except (ValueError, OSError) as error:
        return failure_response(
            request, code="SANKA_EXTENSION_EXECUTION_FAILED", message=str(error)
        )


def _apply(
    request: ExtensionRequest,
    root: Path,
    artifacts: Path,
    data: dict[str, Any],
    rendered: Rendered,
) -> ExtensionResponse:
    if data["capture"]["gaps"] or data["gaps"] or not rendered.files:
        raise ValueError("capture has unsupported behavior; inspect plan gaps")
    reviewed = request.configuration.get("extension_plan_hash")
    if not request.reviewed_plan_hash or not reviewed or reviewed != data["plan_hash"]:
        raise ValueError("extension_plan_hash must match the reviewed current plan")
    saved = _safe(root, artifacts / "plan.json")
    if not saved.is_file() or json.loads(saved.read_text()) != data:
        raise ValueError("saved plan differs; plan and review again")
    output = _safe(root, artifacts / OUTPUT)
    if output.exists():
        raise ValueError("output already exists; preserve repairs")
    assets: dict[str, bytes] = {}
    for path, info in rendered.assets.items():
        source = root / info["source"]
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"asset {info['source']} is missing")
        binary = source.read_bytes()
        if hashlib.sha256(binary).hexdigest() != info["sha256"]:
            raise ValueError(f"asset {info['source']} changed since the plan")
        assets[path] = binary
    staging = Path(tempfile.mkdtemp(prefix=".swiftui-", dir=artifacts))
    try:
        for filename, content in rendered.files.items():
            destination = staging / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content)
        for filename, binary in assets.items():
            destination = staging / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(binary)
        # mkdir reserves the destination without replacing concurrent work.
        output.mkdir()
        for source in staging.iterdir():
            os.rename(source, output / source.name)
    finally:
        shutil.rmtree(staging)
    pending = [
        item["module"] for item in rendered.dispositions if item["disposition"] != "native-screen"
    ]
    limitations = list(LIMITATIONS)
    if pending:
        limitations.append(
            "Placeholder screens need manual adaptation before verify can pass: "
            + ", ".join(pending)
        )
    return success_response(
        request,
        data={
            "output": str(output),
            "plan_hash": data["plan_hash"],
            "readiness": data["readiness"],
            "complete_app": False,
        },
        artifacts=[str(output)],
        limitations=limitations,
    )

# SPDX-License-Identifier: Apache-2.0
"""SDK lifecycle dispatch: scan inventories, plan binds patches, apply writes them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from sanka_extensions.code import (
    ExtensionRequest,
    ExtensionResponse,
    JsonValue,
    failure_response,
    success_response,
)

from . import EXTENSION_ID, TARGET, VERSION, advisory
from .patches import DEFAULT_OUTPUT, build_plan, safe_output, write_patch_directory
from .scanner import Report, scan

MAX_DATA_FINDINGS = 50
LIMITATIONS = (
    "Static read of repository files only: deployed binaries, images already built, host "
    "trust stores, appliances and install-time dependency resolution are not inspected.",
)


def _json(value: object) -> JsonValue:
    return cast(JsonValue, json.loads(json.dumps(value)))


def _write(artifacts: Path, name: str, value: object) -> str:
    artifacts.mkdir(parents=True, exist_ok=True)
    path = artifacts / name
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return str(path)


def _summary(report: Report) -> dict[str, JsonValue]:
    return {
        "advisory": advisory.ADVISORY_ID,
        "files_scanned": report.files_scanned,
        "counts": _json(report.counts()),
        "findings": _json([f.as_json() for f in report.findings[:MAX_DATA_FINDINGS]]),
        "findings_total": len(report.findings),
        "patchable": sum(f.edit is not None for f in report.findings),
        "truncated": report.truncated,
    }


def handle(request: ExtensionRequest) -> ExtensionResponse:
    if request.extension_id != EXTENSION_ID or request.extension_version != VERSION:
        raise ValueError("extension identity/version mismatch")
    root = Path(request.project_root)
    artifacts = Path(request.artifact_root)
    if artifacts.is_relative_to(root) and ".sanka" not in artifacts.relative_to(root).parts:
        raise ValueError("in-source artifacts must live under .sanka")
    if artifacts.is_symlink() or artifacts.resolve() != artifacts.absolute():
        raise ValueError("artifact root must not contain symlinks")
    if request.command not in {"scan", "plan", "apply"}:
        return failure_response(
            request,
            code="SANKA_EXTENSION_UNSUPPORTED_COMMAND",
            message=f"{EXTENSION_ID} supports scan, plan and apply",
        )
    report = scan(root)
    if request.command == "scan":
        path = _write(
            artifacts, "trust-scan.json", {"findings": [f.as_json() for f in report.findings]}
        )
        return success_response(
            request,
            data=_summary(report),
            artifacts=[path],
            limitations=LIMITATIONS,
            next_actions=[f"sanka plan --to {TARGET}"],
        )
    plan, patches = build_plan(root, report)
    output = request.configuration.get("output", DEFAULT_OUTPUT)
    if request.command == "plan":
        path = _write(artifacts, "trust-plan.json", plan)
        data = _summary(report)
        data.update(
            {
                "plan_hash": cast(str, plan["plan_hash"]),
                "default_output": DEFAULT_OUTPUT,
                "patch_files": [patch.path for patch in patches],
                "manual_findings": cast(int, plan["manual_findings"]),
            }
        )
        return success_response(
            request,
            data=data,
            artifacts=[path],
            limitations=LIMITATIONS,
            next_actions=["Review the plan, then run sanka apply --plan-hash <plan hash>"],
        )
    reviewed = request.configuration.get("extension_plan_hash")
    if request.reviewed_plan_hash is None or reviewed != plan["plan_hash"]:
        return failure_response(
            request,
            code="TRUST_AUDIT_PLAN_STALE",
            message="The project changed since the reviewed plan; run sanka plan again.",
        )
    target = safe_output(root, output)
    name = target.relative_to(root).as_posix()
    write_patch_directory(target, plan, patches, name, request.configuration.get("force") is True)
    return success_response(
        request,
        data={
            "plan_hash": cast(str, plan["plan_hash"]),
            "output": name,
            "patch_files": [patch.path for patch in patches],
            "manual_findings": cast(int, plan["manual_findings"]),
        },
        artifacts=[str(target)],
        limitations=(*LIMITATIONS, "Project files were not modified; apply the patches yourself."),
        next_actions=[
            f"Read {name}/README.md",
            f"git apply {name}/trust-audit.patch",
        ],
    )

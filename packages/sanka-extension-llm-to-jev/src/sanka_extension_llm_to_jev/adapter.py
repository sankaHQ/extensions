# SPDX-License-Identifier: Apache-2.0
"""SDK lifecycle dispatch; inference never occurs in this process."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from sanka_extensions.code import ExtensionRequest, ExtensionResponse, JsonValue, success_response

from . import EXTENSION_ID, VERSION
from .common import canonical, object_digest, safe_path
from .planning import build_plan
from .scanner import scan_project


def handle(request: ExtensionRequest) -> ExtensionResponse:
    if request.extension_id != EXTENSION_ID or request.extension_version != VERSION:
        raise ValueError("extension identity/version mismatch")
    root = Path(request.project_root)
    artifacts = Path(request.artifact_root)
    # CLI artifact trees inside .sanka are ignored when hashing source. Other
    # in-source destinations could recursively copy candidates into themselves.
    if artifacts.is_relative_to(root) and ".sanka" not in artifacts.relative_to(root).parts:
        raise ValueError("in-source artifacts must live under .sanka")
    if artifacts.is_symlink() or artifacts.resolve() != artifacts.absolute():
        raise ValueError("artifact root must not contain symlinks")
    if request.command not in {"scan", "plan"}:
        from .lifecycle import handle as lifecycle_handle

        return lifecycle_handle(request)
    inventory = scan_project(root)
    artifacts.mkdir(parents=True, exist_ok=True)
    spec_path = request.configuration.get("decision_spec", "jev-decision.json")
    if not isinstance(spec_path, str):
        raise ValueError("configuration.decision_spec must be a relative path")
    decision_path = safe_path(root, spec_path)
    decision = json.loads(decision_path.read_text()) if decision_path.is_file() else None
    plan = None
    if request.command == "plan":
        if decision is None:
            raise ValueError("reviewed decision specification is required for plan")
        plan = build_plan(root, decision, inventory, request.manifest_digest)
    # Inventory binds unavailable digests explicitly; scan can precede review.
    report = {
        **inventory,
        "bindings": {
            "source_digest": inventory["source_digest"],
            "decision_digest": object_digest(decision) if decision is not None else None,
            "plan_hash": plan["plan_hash"] if plan else None,
            "extension_digest": request.manifest_digest,
            "generated_digests": plan["generated_digests"] if plan else {},
        },
    }
    inventory_path = safe_path(artifacts, "inventory.json")
    inventory_path.write_text(canonical(report), encoding="utf-8")
    written = [str(inventory_path)]
    data: dict[str, JsonValue] = {
        "manual_count": inventory["manual_count"],
        "supported_count": sum(item["status"] == "supported" for item in inventory["call_sites"]),
        "source_digest": inventory["source_digest"],
    }
    if plan is not None:
        plan_path = safe_path(artifacts, "migration-plan.json")
        diff_path = safe_path(artifacts, "migration.diff")
        plan_path.write_text(canonical(plan), encoding="utf-8")
        diff_path.write_text(plan["diff"], encoding="utf-8")
        written.extend([str(plan_path), str(diff_path)])
        data["plan_hash"] = plan["plan_hash"]
        data["bindings"] = cast(JsonValue, plan["bindings"])
    limitations = [
        "Offline static and compatibility evidence does not establish model quality or savings."
    ]
    if inventory["manual_count"]:
        limitations.append(
            f"{inventory['manual_count']} manual call site(s) remain unchanged and unresolved."
        )
    return success_response(request, data=data, artifacts=written, limitations=limitations)

# SPDX-License-Identifier: Apache-2.0
"""Deterministic reviewed candidate contents and source/spec/inventory bindings."""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from . import EXTENSION_ID, VERSION
from .common import digest, object_digest, safe_path
from .decision import validate_spec


def build_plan(
    root: Path, spec: dict[str, Any], inventory: dict[str, Any], manifest_digest: str
) -> dict[str, Any]:
    from .generator import render_candidate

    decision = validate_spec(spec, inventory)
    changes = render_candidate(root, decision, inventory)
    if not changes:
        raise ValueError("candidate generator returned no changes")
    diffs: list[str] = []
    for relative, text in sorted(changes.items()):
        path = safe_path(root, relative)
        before = path.read_text(encoding="utf-8") if path.exists() else ""
        diffs.extend(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                text.splitlines(keepends=True),
                fromfile=f"source/{relative}",
                tofile=f"candidate/{relative}",
            )
        )
    site = next(
        item for item in inventory["call_sites"] if item["id"] == decision["source"]["call_site"]
    )
    plan = {
        "schema_version": "sanka-jev-plan/v1",
        "bindings": {
            "source_digest": inventory["source_digest"],
            "decision_digest": object_digest(decision),
            "inventory_digest": object_digest(inventory),
            "extension_digest": manifest_digest,
            "extension_id": EXTENSION_ID,
            "extension_version": VERSION,
        },
        "decision": decision,
        "call_site": site,
        "changed_files": dict(sorted(changes.items())),
        "generated_digests": {path: digest(text) for path, text in sorted(changes.items())},
        "manual_call_sites": [
            {
                **item,
                "status": "manual",
                "reasons": item["reasons"] or ["call site not selected by reviewed decision"],
            }
            for item in inventory["call_sites"]
            if item["id"] != site["id"]
        ],
        "dependencies": {
            "destination_add": [decision["target"]["sdk"]],
            "source_dependencies": "preserved; OpenAI may serve remaining call sites",
        },
        "behavioral_differences": [
            "Provider and decision semantics change; quality and economics are unverified.",
            "Application-owned uncalibrated confidence gate returns the existing unknown label.",
            "Timeout, malformed output and exhausted attempts return the reviewed fallback.",
            "No implicit OpenAI fallback or double provider call.",
        ],
        "diff": "".join(diffs),
    }
    plan["plan_hash"] = object_digest(plan)
    return plan

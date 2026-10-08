# SPDX-License-Identifier: Apache-2.0
"""Verify an edited Go HTTP server without claiming it was generated or captured."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from sanka_code_migration.drf.scan import _infer_settings_module

from sanka_drf_replay.replay import (
    DEFAULT_IGNORED_TABLES,
    ReplayError,
    edge_probes_from_scan,
    load_scenarios,
    replay,
    save_report,
)
from sanka_extensions.code import (
    ExtensionRequest,
    ExtensionResponse,
    failure_response,
    success_response,
)

from .replay import _source_python


def handle_replay(request: ExtensionRequest) -> ExtensionResponse:
    root, artifacts = Path(request.project_root), Path(request.artifact_root)
    config = request.configuration

    def text(name: str, default: str = "") -> str:
        value = config.get(name, default)
        if not isinstance(value, str) or not value:
            raise ReplayError(f"{name} must be a nonempty string")
        return value

    def path(name: str, default: str = "") -> Path:
        value = root / text(name, default)
        if value.is_symlink() or any(p.is_symlink() for p in value.parents):
            raise ReplayError(f"{name} must not contain symlinks")
        if not value.resolve().is_relative_to(root):
            raise ReplayError(f"{name} must stay inside the source project")
        return value.resolve()

    try:
        cases = load_scenarios(path("scenarios"))
        if config.get("edge_probes"):
            scan_path = artifacts / "scan.json"
            if not scan_path.is_file():
                raise ReplayError("edge probes require a prior scan")
            scan = json.loads(scan_path.read_text())
            for route in scan.get("routes", []):
                route["path"] = re.sub(r":([A-Za-z_][A-Za-z_0-9]*)", r"{\1}", route["path"])
            cases += edge_probes_from_scan(scan, cases)
        if config.get("database_backend", "sqlite") != "sqlite":
            raise ReplayError("Go candidate replay currently requires SQLite")
        ignored = config.get("ignore_tables", list(DEFAULT_IGNORED_TABLES))
        if not isinstance(ignored, list) or any(not isinstance(x, str) for x in ignored):
            raise ReplayError("ignore_tables must be an array of strings")
        for flag in ("all_headers", "edge_probes"):
            if not isinstance(config.get(flag, False), bool):
                raise ReplayError(f"{flag} must be boolean")
        report = replay(
            root,
            cases,
            settings_module=text("settings_module")
            if config.get("settings_module")
            else _infer_settings_module(root),
            candidate_root=path("candidate", "."),
            entrypoint=text("entrypoint", "cmd/api/main.go"),
            db_env=text("db_env", "SANKA_TEST_DB"),
            candidate_db_env=text("candidate_db_env", "DATABASE_URL"),
            seed=path("seed") if config.get("seed") else None,
            python=Path(text("python")) if config.get("python") else Path(_source_python()),
            target="go",
            ignored_tables=[x for x in ignored if isinstance(x, str)],
            all_headers=bool(config.get("all_headers", False)),
        )
        data: dict[str, Any] = save_report(report, artifacts)
        data.update(
            candidate_digest=report["candidate_digest"],
            verification_scope=report["verification_scope"],
        )
        (artifacts / "verify.json").write_text(json.dumps(data, sort_keys=True) + "\n")
        if not report["ok"]:
            return replace(
                failure_response(
                    request,
                    code="SANKA_EXTENSION_REPLAY_MISMATCH",
                    message="Go candidate differs from source behavior",
                    details=data,
                ),
                data=data,
                artifacts=(data["report_path"],),
            )
        return success_response(
            request,
            data=data,
            artifacts=(data["report_path"],),
            limitations=[report["verification_scope"]],
        )
    except ReplayError as error:
        return failure_response(
            request,
            code="SANKA_EXTENSION_REPLAY_INVALID",
            message=str(error),
            details={"failure_category": error.category},
        )

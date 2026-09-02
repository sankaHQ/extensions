# SPDX-License-Identifier: Apache-2.0
"""Map the versioned subprocess contract onto the existing migration engine."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast

from sanka_extension_drf_to_fastapi.django_fastapi import (
    NATIVE_STRATEGY,
    SCAN_FILE,
    _infer_settings_module,
    apply_fastapi_plan,
    load_fastapi_plan,
    plan_fastapi,
    scan_django,
    verify_fastapi_migration,
    write_bench_candidate,
    write_gap_report,
)
from sanka_extension_drf_to_fastapi.fastapi_tests import test_fastapi_app
from sanka_extension_drf_to_fastapi.replay import (
    DEFAULT_DB_ENV,
    DEFAULT_ENTRYPOINT,
    DEFAULT_IGNORED_TABLES,
    ReplayError,
    edge_probes_from_scan,
    load_scenarios,
    replay,
)
from sanka_extension_sdk import (
    ExtensionRequest,
    ExtensionResponse,
    JsonValue,
    failure_response,
    success_response,
)

_PLAN_INPUTS = ("generation", "output", "package_manager", "strategy")


def _string(configuration: dict[str, JsonValue], name: str) -> str:
    value = configuration.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"configuration.{name} must be a non-empty string")
    return value


def _optional_string(configuration: dict[str, JsonValue], name: str) -> str | None:
    return None if configuration.get(name) is None else _string(configuration, name)


def _boolean(configuration: dict[str, JsonValue], name: str, default: bool = False) -> bool:
    value = configuration.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"configuration.{name} must be a boolean")
    return value


def _number(configuration: dict[str, JsonValue], name: str, default: float) -> float:
    value = configuration.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"configuration.{name} must be a number")
    return float(value)


def _plan_inputs(configuration: dict[str, JsonValue]) -> list[JsonValue]:
    return [name for name in _PLAN_INPUTS if not isinstance(configuration.get(name), str)]


def _artifact(request: ExtensionRequest, name: str) -> str:
    return str((Path(request.artifact_root) / name).resolve())


def _artifacts(request: ExtensionRequest, values: Iterable[str | Path]) -> tuple[str, ...]:
    roots = [Path(request.artifact_root).resolve()]
    configured_output = request.configuration.get("output")
    root_values = [configured_output, request.configuration.get("bench_candidate")]
    for value in root_values:
        if not isinstance(value, str) or not value:
            continue
        root = Path(value)
        roots.append((root if root.is_absolute() else Path(request.project_root) / root).resolve())
    artifacts = tuple(str(Path(value).resolve()) for value in values)
    outside = [
        artifact
        for artifact in map(Path, artifacts)
        if not any(artifact == root or artifact.is_relative_to(root) for root in roots)
    ]
    if (
        outside
        and not isinstance(configured_output, str)
        and request.command
        in {
            "apply",
            "test",
            "verify",
        }
    ):
        plan = load_fastapi_plan(request.project_root, artifact_dir=request.artifact_root)
        root = Path(plan.default_output)
        roots.append((root if root.is_absolute() else Path(request.project_root) / root).resolve())
        outside = [
            artifact
            for artifact in outside
            if not any(artifact == root or artifact.is_relative_to(root) for root in roots)
        ]
    if outside:
        raise ValueError(f"artifact path is outside allowed roots: {outside[0]}")
    return artifacts


def _json_value(value: object) -> JsonValue:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("extension data contains a non-finite number")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("extension data mappings require string keys")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence):
        return [_json_value(item) for item in value]
    raise TypeError(f"unsupported extension data value: {type(value).__name__}")


def _data(payload: Mapping[str, object]) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], _json_value(payload))


def _handle_plan(request: ExtensionRequest) -> ExtensionResponse:
    if missing := _plan_inputs(request.configuration):
        return failure_response(
            request,
            code="SANKA_EXTENSION_INPUT_REQUIRED",
            message="DRF-to-FastAPI plan configuration is incomplete",
            details={"inputs": missing},
        )
    plan = plan_fastapi(
        request.project_root,
        artifact_dir=request.artifact_root,
        output=_string(request.configuration, "output"),
        strategy=_string(request.configuration, "strategy"),
        sql_engine=_optional_string(request.configuration, "orm"),
        generation_mode=_string(request.configuration, "generation"),
        package_manager=_string(request.configuration, "package_manager"),
    )
    return success_response(
        request,
        data=_data(plan.to_dict()),
        artifacts=_artifacts(request, [_artifact(request, "plan-fastapi.json")]),
    )


def _handle_apply(request: ExtensionRequest) -> ExtensionResponse:
    configuration = request.configuration
    missing: list[JsonValue] = []
    if request.reviewed_plan_hash is None:
        missing.append("reviewed_plan_hash")
    if not isinstance(configuration.get("extension_plan_hash"), str):
        missing.append("extension_plan_hash")
    if missing:
        return failure_response(
            request,
            code="SANKA_EXTENSION_INPUT_REQUIRED",
            message="DRF-to-FastAPI apply requires a reviewed plan",
            details={"inputs": missing},
        )

    extension_plan_hash = _string(configuration, "extension_plan_hash")
    plan = load_fastapi_plan(request.project_root, artifact_dir=request.artifact_root)
    if extension_plan_hash != plan.plan_hash:
        return failure_response(
            request,
            code="SANKA_EXTENSION_PLAN_HASH_MISMATCH",
            message="reviewed extension plan does not match current plan",
            details={"current": plan.plan_hash, "reviewed": extension_plan_hash},
        )

    minimum = _number(configuration, "min_readiness", 50.0)
    if not 0 <= minimum <= 100:
        return failure_response(
            request,
            code="SANKA_EXTENSION_INPUT_INVALID",
            message="min_readiness must be between 0 and 100",
        )
    if plan.mode == NATIVE_STRATEGY:
        gap_only = _boolean(configuration, "gap_report_only")
        below_minimum = plan.readiness * 100.0 < minimum
        if gap_only or below_minimum or plan.native_routes == 0:
            destination = (
                _optional_string(configuration, "bench_candidate")
                or _optional_string(configuration, "output")
                or str(Path(request.artifact_root) / "gap-report")
            )
            report = write_gap_report(
                request.project_root,
                destination,
                artifact_dir=request.artifact_root,
            ).resolve()
            if gap_only:
                return success_response(
                    request,
                    data={
                        "gap_report": str(report),
                        "plan_hash": plan.plan_hash,
                        "readiness": plan.readiness,
                    },
                    artifacts=_artifacts(request, [report]),
                )
            reason = (
                "the native plan contains no generatable routes"
                if plan.native_routes == 0
                else f"readiness is below min_readiness {minimum:g}%"
            )
            response = failure_response(
                request,
                code="SANKA_EXTENSION_READINESS",
                message=reason,
                details={
                    "gap_report": str(report),
                    "plan_hash": plan.plan_hash,
                    "readiness": plan.readiness,
                },
            )
            return replace(
                response,
                artifacts=_artifacts(request, [report]),
                limitations=(reason,),
            )
    elif _boolean(configuration, "gap_report_only"):
        return failure_response(
            request,
            code="SANKA_EXTENSION_INPUT_INVALID",
            message="gap reports require a native plan",
        )

    output, routes = apply_fastapi_plan(
        request.project_root,
        artifact_dir=request.artifact_root,
        output=_optional_string(configuration, "output"),
        plan_hash=extension_plan_hash,
        force=_boolean(configuration, "force"),
        sql_engine=_optional_string(configuration, "orm"),
    )
    artifacts = [str(output.resolve())]
    if candidate_root := _optional_string(configuration, "bench_candidate"):
        candidate = write_bench_candidate(
            request.project_root,
            candidate_root,
            artifact_dir=request.artifact_root,
        ).resolve()
        artifacts.append(str(candidate))
    return success_response(
        request,
        data={
            "output": str(output.resolve()),
            "routes_generated": routes,
            "mode": plan.mode,
            "generation_mode": plan.generation_mode,
            "database_required": plan.database_required,
            "sql_engine": plan.sql_engine if plan.database_required else None,
            "plan_hash": plan.plan_hash,
        },
        artifacts=_artifacts(request, artifacts),
        limitations=(
            [f"{plan.needs_adaptation_routes} route(s) need manual adaptation"]
            if plan.needs_adaptation_routes
            else []
        ),
        next_actions=["test"],
    )


def _handle_test(request: ExtensionRequest) -> ExtensionResponse:
    result = test_fastapi_app(
        request.project_root,
        artifact_dir=request.artifact_root,
        output=_optional_string(request.configuration, "output"),
    )
    artifacts = [
        str(Path(value).resolve())
        for name in ("file", "environment", "pyproject", "lockfile")
        if isinstance((value := result.get(name)), str)
    ]
    response = success_response if result.get("ok") else failure_response
    if response is failure_response:
        return replace(
            failure_response(
                request,
                code="SANKA_EXTENSION_TEST_FAILED",
                message="generated FastAPI tests failed",
                details=_data(result),
            ),
            artifacts=_artifacts(request, artifacts),
        )
    return success_response(
        request,
        data=_data(result),
        artifacts=_artifacts(request, artifacts),
        next_actions=["verify"],
    )


def _string_list(configuration: dict[str, JsonValue], name: str) -> list[str] | None:
    value = configuration.get(name)
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"configuration.{name} must be an array of strings")
    return [str(item) for item in value]


def _project_path(request: ExtensionRequest, value: str) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else Path(request.project_root) / path).resolve()


def _replay_settings_module(request: ExtensionRequest) -> str:
    explicit = _optional_string(request.configuration, "settings_module")
    if explicit:
        return explicit
    scan_path = Path(request.artifact_root) / SCAN_FILE
    if scan_path.is_file():
        try:
            payload = json.loads(scan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("settings_module"), str):
            return str(payload["settings_module"])
    return _infer_settings_module(Path(request.project_root))


def _handle_replay(request: ExtensionRequest) -> ExtensionResponse:
    """Differential scenario replay; independent of any plan or generated manifest."""
    configuration = request.configuration
    scenarios_path = _project_path(request, _string(configuration, "scenarios"))
    candidate_value = _optional_string(configuration, "candidate")
    candidate_root = (
        _project_path(request, candidate_value) if candidate_value else Path(request.project_root)
    )
    seed_value = _optional_string(configuration, "seed")
    ignored = _string_list(configuration, "ignore_tables")
    try:
        scenarios = load_scenarios(scenarios_path)
        if _boolean(configuration, "edge_probes"):
            scan_path = Path(request.artifact_root) / SCAN_FILE
            if not scan_path.is_file():
                raise ReplayError(
                    "--edge-probes needs a scan artifact; run `sanka scan` first or omit the flag"
                )
            scan_payload = json.loads(scan_path.read_text(encoding="utf-8"))
            scenarios = [*scenarios, *edge_probes_from_scan(scan_payload)]
        report = replay(
            Path(request.project_root),
            scenarios,
            settings_module=_replay_settings_module(request),
            candidate_root=candidate_root,
            entrypoint=_optional_string(configuration, "entrypoint") or DEFAULT_ENTRYPOINT,
            db_env=_optional_string(configuration, "db_env") or DEFAULT_DB_ENV,
            seed=_project_path(request, seed_value) if seed_value else None,
            ignored_tables=tuple(ignored) if ignored is not None else DEFAULT_IGNORED_TABLES,
            all_headers=_boolean(configuration, "all_headers"),
            python=(
                Path(_string(configuration, "python"))
                if configuration.get("python") is not None
                else None
            ),
            candidate_python=(
                Path(_string(configuration, "candidate_python"))
                if configuration.get("candidate_python") is not None
                else None
            ),
        )
    except ReplayError as error:
        return failure_response(
            request,
            code="SANKA_EXTENSION_REPLAY_INVALID",
            message=str(error),
        )
    data = _data(report)
    if not report["ok"]:
        return replace(
            failure_response(
                request,
                code="SANKA_EXTENSION_REPLAY_MISMATCH",
                message="scenario replay found differences between the source and the candidate",
                details=data,
            ),
            data=data,
            limitations=tuple(str(line) for line in report["summary_lines"]),
        )
    return success_response(
        request,
        data=data,
        limitations=tuple(str(line) for line in report["summary_lines"][1:]),
    )


def _handle_verify(request: ExtensionRequest) -> ExtensionResponse:
    result = verify_fastapi_migration(
        request.project_root,
        artifact_dir=request.artifact_root,
        output=_optional_string(request.configuration, "output"),
        probe_http=not _boolean(request.configuration, "no_http"),
        cases=_optional_string(request.configuration, "cases"),
    )
    paths = result.get("paths")
    values = (
        [
            paths.get(name)
            for name in (
                "scan",
                "plan",
                "generated",
                "manifest",
                "pyproject",
                "environment",
                "lockfile",
            )
        ]
        if isinstance(paths, dict)
        else []
    )
    generated_files = result.get("generated_files")
    if isinstance(generated_files, list):
        values.extend(generated_files)
    artifacts = [str(Path(value).resolve()) for value in values if isinstance(value, str)]
    if not result.get("ok"):
        return replace(
            failure_response(
                request,
                code="SANKA_EXTENSION_VERIFICATION_FAILED",
                message="FastAPI migration verification failed",
                details=_data(result),
            ),
            artifacts=_artifacts(request, artifacts),
        )
    return success_response(request, data=_data(result), artifacts=_artifacts(request, artifacts))


def handle(request: ExtensionRequest) -> ExtensionResponse:
    """Execute one extension lifecycle command without writing to stdout."""
    try:
        if request.command == "scan":
            scan = scan_django(
                request.project_root,
                settings_module=_optional_string(request.configuration, "settings_module"),
                artifact_dir=request.artifact_root,
            )
            return success_response(
                request,
                data=_data(scan.to_dict()),
                artifacts=_artifacts(request, [_artifact(request, "scan.json")]),
            )
        if request.command == "plan":
            return _handle_plan(request)
        if request.command == "apply":
            return _handle_apply(request)
        if request.command == "test":
            return _handle_test(request)
        if request.command == "verify":
            if request.configuration.get("scenarios") is not None:
                return _handle_replay(request)
            return _handle_verify(request)
        return failure_response(
            request,
            code="SANKA_EXTENSION_UNSUPPORTED_COMMAND",
            message=f"unsupported command: {request.command}",
        )
    except Exception as error:
        return failure_response(
            request,
            code="SANKA_EXTENSION_EXECUTION_FAILED",
            message=str(error),
        )

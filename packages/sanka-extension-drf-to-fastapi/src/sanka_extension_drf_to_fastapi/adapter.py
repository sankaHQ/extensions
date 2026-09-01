# SPDX-License-Identifier: Apache-2.0
"""Map the versioned subprocess contract onto the existing migration engine."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast

from sanka_extension_drf_to_fastapi.django_fastapi import (
    NATIVE_STRATEGY,
    apply_fastapi_plan,
    load_fastapi_plan,
    plan_fastapi,
    scan_django,
    verify_fastapi_migration,
    write_bench_candidate,
    write_gap_report,
)
from sanka_extension_drf_to_fastapi.fastapi_tests import test_fastapi_app
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


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
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
        artifacts=[_artifact(request, "plan-fastapi.json")],
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
                    artifacts=[str(report)],
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
            return replace(response, artifacts=(str(report),), limitations=(reason,))
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
        artifacts=artifacts,
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
            artifacts=tuple(artifacts),
        )
    return success_response(
        request, data=_data(result), artifacts=artifacts, next_actions=["verify"]
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
                "python",
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
            artifacts=tuple(artifacts),
        )
    return success_response(request, data=_data(result), artifacts=artifacts)


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
                artifacts=[_artifact(request, "scan.json")],
            )
        if request.command == "plan":
            return _handle_plan(request)
        if request.command == "apply":
            return _handle_apply(request)
        if request.command == "test":
            return _handle_test(request)
        if request.command == "verify":
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

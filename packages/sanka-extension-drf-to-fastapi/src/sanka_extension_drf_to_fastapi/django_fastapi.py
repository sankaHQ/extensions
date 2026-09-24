# SPDX-License-Identifier: Apache-2.0
"""Django REST Framework to FastAPI migration recipes.

Two strategies share one scan artifact:

- ``native`` (default): generate a genuinely native async FastAPI request
  layer for the supported DRF envelope (ModelViewSet CRUD over
  ModelSerializer fields whose semantics the scan captured, plus the router
  API root). Persistence is async SQL (Tortoise by default; SQLAlchemy or
  psycopg on request) against the existing Django tables. Django is not
  imported at serve time. Format-suffix alias routes are dropped as a
  disclosed contract change, and routes outside the envelope are reported
  as needing manual adaptation.
- ``compatibility``: the strangler bridge from v0.1. It creates a real FastAPI
  route graph while dispatching each route into the existing Django
  application in-process, preserving observable behavior for the whole route
  surface at the cost of still serving through DRF.

A compatibility bridge must never support the claim that DRF was replaced;
``sanka verify`` and Sanka Migration Bench treat only the native strategy as a
completed migration.
"""

from __future__ import annotations

import ast
import base64
import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace as replace_dataclass
from pathlib import Path
from typing import Any, cast

from sanka_code_migration.drf.scan import (  # isort: skip
    _KNOWN_SAFE_MIDDLEWARE as _KNOWN_SAFE_MIDDLEWARE,
    _MESSAGE_KEYS as _MESSAGE_KEYS,
    _OPERATION_OVERRIDES as _OPERATION_OVERRIDES,
    _SEARCH_LOOKUPS as _SEARCH_LOOKUPS,
    _SUPPORTED_VIEWSET_ACTIONS as _SUPPORTED_VIEWSET_ACTIONS,
    _UNSUPPORTED_SEARCH_PREFIXES as _UNSUPPORTED_SEARCH_PREFIXES,
    _VIEW_CARRYOVER_MODULES as _VIEW_CARRYOVER_MODULES,
    DEFAULT_ARTIFACT_DIR as DEFAULT_ARTIFACT_DIR,
    SCAN_FILE as SCAN_FILE,
    FrameworkMigrationError as FrameworkMigrationError,
    _adaptation_reason as _adaptation_reason,
    _analyze_create_carryover as _analyze_create_carryover,
    _analyze_view_carryover as _analyze_view_carryover,
    _api_root_links as _api_root_links,
    _artifact_path as _artifact_path,
    _attr_chain as _attr_chain,
    _bootstrap_django as _bootstrap_django,
    _build_serializer_ir as _build_serializer_ir,
    _capture_database as _capture_database,
    _capture_http_security as _capture_http_security,
    _count_test_files as _count_test_files,
    _custom_lookup_adaptation_reason as _custom_lookup_adaptation_reason,
    _defines_custom_validation as _defines_custom_validation,
    _defines_methods as _defines_methods,
    _django_field_default as _django_field_default,
    _field_messages as _field_messages,
    _field_timezone_name as _field_timezone_name,
    _generic_actions as _generic_actions,
    _generic_messages as _generic_messages,
    _generic_view_adaptation_reason as _generic_view_adaptation_reason,
    _infer_settings_module as _infer_settings_module,
    _is_docstring as _is_docstring,
    _is_drf_view as _is_drf_view,
    _is_format_alias_path as _is_format_alias_path,
    _is_request_user as _is_request_user,
    _is_safe_method_check as _is_safe_method_check,
    _listing_support as _listing_support,
    _match_owner_permission as _match_owner_permission,
    _match_perform_create as _match_perform_create,
    _match_update_drop as _match_update_drop,
    _maybe_int as _maybe_int,
    _middleware_adaptation_reason as _middleware_adaptation_reason,
    _native_route_support as _native_route_support,
    _nested_many_field_ir as _nested_many_field_ir,
    _not_native as _not_native,
    _obj_owner_attr as _obj_owner_attr,
    _ownership_field as _ownership_field,
    _probe_auth_messages as _probe_auth_messages,
    _probe_ordering_rule as _probe_ordering_rule,
    _qualified_items as _qualified_items,
    _qualified_name as _qualified_name,
    _read_json as _read_json,
    _regex_is_single_path_segment as _regex_is_single_path_segment,
    _related_attname as _related_attname,
    _replace_named_regex_groups as _replace_named_regex_groups,
    _route_methods as _route_methods,
    _safe_source as _safe_source,
    _serializer_field_ir as _serializer_field_ir,
    _serializer_ir as _serializer_ir,
    _source_location as _source_location,
    _status_codes as _status_codes,
    _to_fastapi_path as _to_fastapi_path,
    _unsupported_middleware as _unsupported_middleware,
    _user_fk_attname as _user_fk_attname,
    _view_auth_support as _view_auth_support,
    _viewset_overrides as _viewset_overrides,
    _walk_patterns as _walk_patterns,
    _WalkResult as _WalkResult,
    _write_json as _write_json,
    _write_text as _write_text,
    load_framework_scan as load_framework_scan,
    scan_django as scan_django,
)

from sanka_extension_drf_to_fastapi.generated_environment import (
    GeneratedEnvironment,
    ensure_generated_environment,
)
from sanka_extension_drf_to_fastapi.hashing import content_hash
from sanka_extension_drf_to_fastapi.model import (
    FileOperation,
    FrameworkPlan,
    FrameworkScan,
    ParityNote,
    PlannedRoute,
    RouteAdaptationReason,
    RouteIR,
    SerializerFieldIR,
    SerializerIR,
)
from sanka_extension_drf_to_fastapi.native_async import (
    SQL_ENGINE_LABELS,
    render_async_sql_files,
    render_generated_pyproject,
    resolve_sql_engine,
)

DEFAULT_FASTAPI_OUTPUT = ".sanka/output/fastapi"
PLAN_FILE = "plan-fastapi.json"
GENERATED_MANIFEST = "sanka-manifest.json"
PROJECT_MANIFEST = ".sanka/generated-manifest.json"
GENERATION_MODES = ("full", "update", "minimal")
PACKAGE_MANAGERS = ("uv", "pip")

NATIVE_STRATEGY = "native"
COMPATIBILITY_STRATEGY = "compatibility"
ROUTE_STRATEGY_NATIVE_CRUD = "native-fastapi-crud"
ROUTE_STRATEGY_NATIVE_API_ROOT = "native-fastapi-api-root"
ROUTE_STRATEGY_DROPPED_ALIAS = "dropped-format-suffix-alias"
ROUTE_STRATEGY_BRIDGE = "django-in-process-compatibility-bridge"
ROUTE_STRATEGY_MANUAL = "needs-manual-adaptation"


def _resolve_output(root: Path, output: str | Path) -> Path:
    path = Path(output)
    return (path if path.is_absolute() else root / path).resolve()


def _planned_output_files(
    *,
    layout: str,
    strategy: str,
    database_required: bool,
    sql_engine: str,
) -> tuple[str, ...]:
    native_runtime = ["sanka_native.py"]
    if database_required:
        native_runtime.append("sanka_store.py")
    if database_required and sql_engine not in {"psycopg", "django"}:
        native_runtime.append("models.py")
    if database_required and sql_engine == "django":
        native_runtime.append("sanka_settings.py")
    runtime = native_runtime if strategy == NATIVE_STRATEGY else ["sanka_compat.py"]
    metadata = ["README.md", "requirements.txt", "requirements-test.txt", "pyproject.toml"]
    if layout == "minimal":
        return tuple(sorted(["app.py", GENERATED_MANIFEST, *runtime, *metadata]))
    generated = [f"app/generated/{name}" for name in runtime]
    files = [
        ".env.example",
        ".gitignore",
        PROJECT_MANIFEST,
        GENERATED_MANIFEST,
        "app/__init__.py",
        "app/api/__init__.py",
        "app/api/health.py",
        "app/api/router.py",
        "app/core/__init__.py",
        "app/core/config.py",
        "app/core/logging.py",
        "app/generated/__init__.py",
        f"app/generated/{GENERATED_MANIFEST}",
        "app/main.py",
        "tests/__init__.py",
        *generated,
        *metadata,
    ]
    if database_required:
        files.append("app/core/database.py")
    return tuple(sorted(files))


def _text_hash(path: Path) -> str:
    return content_hash(path.read_text(encoding="utf-8"))


def _is_manifest_path(name: str) -> bool:
    return name in (GENERATED_MANIFEST, PROJECT_MANIFEST) or name.endswith(f"/{GENERATED_MANIFEST}")


def _target_fingerprint(output: Path, manifest: dict[str, Any]) -> str:
    recorded = dict(manifest.get("generated_file_hashes") or {})
    actual = {
        name: _text_hash(output / name) if (output / name).is_file() else "missing"
        for name in sorted(recorded)
    }
    return content_hash({"manifest": manifest, "files": actual})


def _plan_file_operations(
    output: Path,
    expected_files: tuple[str, ...],
    *,
    generation_mode: str,
) -> tuple[str, str, tuple[FileOperation, ...]]:
    if generation_mode != "update":
        return (
            "",
            "",
            tuple(
                FileOperation(
                    path=name,
                    action="conflict" if (output / name).exists() else "create",
                    expected_hash=_text_hash(output / name) if (output / name).is_file() else "",
                )
                for name in expected_files
            ),
        )
    manifest_path = output / GENERATED_MANIFEST
    if not manifest_path.is_file():
        raise FrameworkMigrationError(
            f"update target is not a Sanka-generated project: {output}; "
            "choose full or minimal generation"
        )
    manifest = _read_json(manifest_path, label="generated target manifest")
    target_mode = str(manifest.get("generation_mode") or "minimal")
    if target_mode not in {"full", "minimal"}:
        raise FrameworkMigrationError(f"unsupported generated target mode: {target_mode}")
    recorded = dict(manifest.get("generated_file_hashes") or {})
    if not recorded:
        raise FrameworkMigrationError(
            "the generated target predates safe update metadata; regenerate it once with "
            "full or minimal mode"
        )
    operations: list[FileOperation] = []
    for name in expected_files:
        path = output / name
        expected_hash = str(recorded.get(name) or "")
        if not path.exists():
            action = "create"
        elif _is_manifest_path(name):
            action = "modify"
            expected_hash = _text_hash(path)
        elif not expected_hash or _text_hash(path) != expected_hash:
            action = "conflict"
        else:
            action = "modify"
        operations.append(FileOperation(path=name, action=action, expected_hash=expected_hash))
    return target_mode, _target_fingerprint(output, manifest), tuple(operations)


def _preview_update_operations(
    plan: FrameworkPlan,
    scan: FrameworkScan,
    output: Path,
    *,
    source_root: Path,
    layout: str,
    sql_engine: str,
) -> tuple[FileOperation, ...]:
    target_manifest = _read_json(output / GENERATED_MANIFEST, label="generated target manifest")
    recorded = dict(target_manifest.get("generated_file_hashes") or {})
    old_routes = {
        f"{str(route.get('method')).upper()} {route.get('path')}"
        for route in target_manifest.get("routes", [])
    }
    new_routes = {
        route.key
        for route in plan.routes
        if route.automatic and route.strategy != ROUTE_STRATEGY_DROPPED_ALIAS
    }
    removed_routes = old_routes - new_routes
    entrypoint = str(target_manifest.get("entrypoint") or "app.py")
    with tempfile.TemporaryDirectory(prefix="sanka-plan-") as temporary:
        staged = Path(temporary)
        _render_fastapi_output(
            plan,
            scan,
            staged,
            layout=layout,
            source_root=os.path.relpath(source_root, output),
            sql_engine=sql_engine,
        )
        operations: list[FileOperation] = []
        for operation in plan.file_operations:
            current = output / operation.path
            recorded_hash = str(recorded.get(operation.path) or "")
            if _is_manifest_path(operation.path):
                recorded_hash = _text_hash(current) if current.is_file() else ""
            if not current.exists():
                action = "create"
            elif not recorded_hash or _text_hash(current) != recorded_hash:
                action = "conflict"
            elif _is_manifest_path(operation.path) or operation.path == "README.md":
                action = "modify"
            elif operation.path == entrypoint and removed_routes:
                action = "conflict"
            else:
                candidate = staged / operation.path
                action = (
                    "unchanged"
                    if candidate.is_file() and _text_hash(candidate) == _text_hash(current)
                    else "modify"
                )
            operations.append(
                FileOperation(
                    path=operation.path,
                    action=action,
                    expected_hash=_text_hash(current) if current.is_file() else "",
                )
            )
    return tuple(operations)


def plan_fastapi(
    root: str | Path = ".",
    *,
    artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR,
    output: str = DEFAULT_FASTAPI_OUTPUT,
    strategy: str = NATIVE_STRATEGY,
    sql_engine: str | None = None,
    generation_mode: str = "minimal",
    package_manager: str | None = None,
    swagger_ui: bool | None = None,
) -> FrameworkPlan:
    if swagger_ui is not None and not isinstance(swagger_ui, bool):
        raise FrameworkMigrationError("swagger_ui must be a boolean")
    selected_swagger_ui = True if swagger_ui is None else swagger_ui
    if strategy not in (NATIVE_STRATEGY, COMPATIBILITY_STRATEGY):
        raise FrameworkMigrationError(f"unknown plan strategy: {strategy}")
    if generation_mode not in GENERATION_MODES:
        raise FrameworkMigrationError(
            f"unknown generation mode: {generation_mode}; choose {', '.join(GENERATION_MODES)}"
        )
    if package_manager is not None and package_manager not in PACKAGE_MANAGERS:
        raise FrameworkMigrationError(
            f"unknown package manager: {package_manager}; choose {', '.join(PACKAGE_MANAGERS)}"
        )
    selected_package_manager = package_manager or "uv"
    root_path = Path(root).resolve()
    scan = load_framework_scan(root_path, artifact_dir=artifact_dir)
    if strategy == NATIVE_STRATEGY:
        routes = tuple(
            _plan_native_route(route, middleware=scan.middleware) for route in scan.routes
        )
        database_required = any(route.strategy == ROUTE_STRATEGY_NATIVE_CRUD for route in routes)
        try:
            engine = resolve_sql_engine(sql_engine) if database_required else "none"
        except ValueError as error:
            raise FrameworkMigrationError(str(error)) from error
        if engine == "psycopg" and scan.database.vendor != "postgresql":
            raise FrameworkMigrationError(
                "psycopg requires PostgreSQL; this project's database is "
                + (scan.database.vendor or "unknown")
            )
        retained = (
            (
                "Existing Django tables (schema reused, not rewritten)"
                if database_required
                else "No source database is required by generated routes"
            ),
            (
                f"Async SQL via {SQL_ENGINE_LABELS[engine]}"
                if database_required
                else "No database runtime required by generated routes"
            ),
            "DRF removed; FastAPI async request layer (Django is not imported at serve time)",
            "Format-suffix alias routes dropped (disclosed contract change)",
        )
    else:
        database_required = False
        engine = "django"
        routes = tuple(
            PlannedRoute(
                method=route.method,
                path=route.path,
                operation=route.operation,
                source_view=route.view,
                strategy=ROUTE_STRATEGY_BRIDGE,
                automatic=route.supported,
                parity_notes=route.parity_notes,
            )
            for route in scan.routes
        )
        retained = (
            "Django models and migrations",
            "Django ORM and synchronous transactions",
            "Django authentication and permissions",
            "DRF handlers behind the generated compatibility bridge",
        )
    output_path = _resolve_output(root_path, output)
    target_generation_mode = ""
    target_fingerprint = ""
    layout = generation_mode
    if generation_mode == "update":
        manifest = _read_json(output_path / GENERATED_MANIFEST, label="generated target manifest")
        if swagger_ui is None:
            selected_swagger_ui = manifest.get("swagger_ui", True)
            if not isinstance(selected_swagger_ui, bool):
                raise FrameworkMigrationError("generated target swagger_ui must be a boolean")
        target_strategy = str(manifest.get("mode") or "")
        if target_strategy and target_strategy != strategy:
            raise FrameworkMigrationError(
                f"update target uses {target_strategy!r} strategy, not {strategy!r}; "
                "generate a new target to change strategy"
            )
        layout = str(manifest.get("generation_mode") or "minimal")
        if package_manager is None:
            selected_package_manager = str(
                manifest.get("package_manager") or selected_package_manager
            )
        if database_required:
            target_engine = str(manifest.get("sql_engine") or "")
            if (
                sql_engine is not None
                and target_engine in {"tortoise", "sqlalchemy", "psycopg"}
                and engine != target_engine
            ):
                raise FrameworkMigrationError(
                    f"update target uses ORM {target_engine!r}, not {engine!r}; "
                    "generate a new target to change ORM"
                )
            if sql_engine is None and target_engine in {"tortoise", "sqlalchemy", "psycopg"}:
                engine = target_engine
    if selected_package_manager not in PACKAGE_MANAGERS:
        raise FrameworkMigrationError(
            f"unsupported generated package manager: {selected_package_manager}"
        )
    if strategy == NATIVE_STRATEGY:
        serializers_by_name = {
            serializer.name: serializer for serializer in scan.serializer_details
        }
        scanned_by_key = {route.key: route for route in scan.routes}
        restricted = []
        for route in routes:
            scanned = scanned_by_key[route.key]
            serializer = serializers_by_name.get(scanned.serializer or "")
            if (
                route.automatic
                and route.operation == "create"
                and serializer is not None
                and serializer.create_style == "carryover"
                and (serializer.create_contract is None or engine != "tortoise")
            ):
                route = replace_dataclass(
                    route,
                    automatic=False,
                    strategy=ROUTE_STRATEGY_MANUAL,
                    adaptation_reasons=(
                        _adaptation_reason(
                            "SANKA_DRF_NESTED_TRANSACTION_UNSUPPORTED",
                            "nested-create",
                            "This custom create requires a supported declarative contract and "
                            "Tortoise transaction; rescan or adapt this route manually.",
                        ),
                    ),
                )
            restricted.append(route)
        routes = tuple(restricted)
    expected_files = _planned_output_files(
        layout=layout,
        strategy=strategy,
        database_required=database_required,
        sql_engine=engine,
    )
    target_generation_mode, target_fingerprint, file_operations = _plan_file_operations(
        output_path,
        expected_files,
        generation_mode=generation_mode,
    )
    capabilities = [
        "fastapi-routes",
        "generated-app-tests",
        f"{selected_package_manager}-environment",
    ]
    omissions: list[str] = []
    if layout == "full":
        capabilities.extend(("settings", "structured-logging", "request-context", "health-check"))
    else:
        omissions.append("full-project-infrastructure")
    if database_required:
        capabilities.extend(("database-configuration", "database-lifecycle", "persistence"))
    else:
        omissions.append("database-runtime")
    if strategy == COMPATIBILITY_STRATEGY:
        capabilities.append("django-compatibility-bridge")
        omissions.append("drf-removal")
    if any(not route.automatic for route in routes):
        omissions.append("manual-route-adaptations")
    plan = FrameworkPlan(
        schema_version=4,
        source_framework=scan.framework,
        target_framework="fastapi",
        mode=strategy,
        source_scan_hash=scan.scan_hash,
        settings_module=scan.settings_module,
        routes=routes,
        risks=scan.risks,
        retained=retained,
        default_output=output,
        sql_engine=engine,
        generation_mode=generation_mode,
        target_generation_mode=target_generation_mode,
        package_manager=selected_package_manager,
        swagger_ui=selected_swagger_ui,
        database_required=database_required,
        target_fingerprint=target_fingerprint,
        file_operations=file_operations,
        capabilities=tuple(capabilities),
        omissions=tuple(omissions),
    ).with_hash()
    if generation_mode == "update" and (plan.mode != NATIVE_STRATEGY or plan.native_routes):
        file_operations = _preview_update_operations(
            plan,
            scan,
            output_path,
            source_root=root_path,
            layout=layout,
            sql_engine=engine,
        )
        plan = replace_dataclass(plan, file_operations=file_operations, plan_hash="").with_hash()
    _write_json(_artifact_path(root_path, artifact_dir, PLAN_FILE), plan.to_dict())
    return plan


def _plan_native_route(route: RouteIR, *, middleware: tuple[str, ...] = ()) -> PlannedRoute:
    adaptation_reasons: tuple[RouteAdaptationReason, ...] = ()
    if _is_format_alias_path(route.path):
        strategy = ROUTE_STRATEGY_DROPPED_ALIAS
        automatic = True
    elif route.native and route.serializer is None:
        strategy = ROUTE_STRATEGY_NATIVE_API_ROOT
        automatic = True
    elif route.native:
        strategy = ROUTE_STRATEGY_NATIVE_CRUD
        automatic = True
    else:
        strategy = ROUTE_STRATEGY_MANUAL
        automatic = False
        adaptation_reasons = route.adaptation_reasons or _legacy_adaptation_reasons(
            route, middleware=middleware
        )
    return PlannedRoute(
        method=route.method,
        path=route.path,
        operation=route.operation,
        source_view=route.view,
        strategy=strategy,
        automatic=automatic,
        adaptation_reasons=adaptation_reasons,
        parity_notes=route.parity_notes,
    )


def _legacy_adaptation_reasons(
    route: RouteIR, *, middleware: tuple[str, ...]
) -> tuple[RouteAdaptationReason, ...]:
    """Explain older scan artifacts that predate per-route reasons."""
    if not route.supported:
        return (
            _adaptation_reason(
                "SANKA_DRF_ROUTE_PATTERN_UNSUPPORTED",
                "route-pattern",
                "The route pattern cannot be represented safely as a FastAPI path.",
            ),
        )
    unsupported_middleware = _unsupported_middleware(middleware)
    if unsupported_middleware:
        return (_middleware_adaptation_reason(unsupported_middleware),)
    return (
        _adaptation_reason(
            "SANKA_DRF_NATIVE_DETAIL_RESCAN_REQUIRED",
            "scan-artifact",
            "This older scan did not record the native disqualifier; run `sanka scan` again.",
        ),
    )


def load_fastapi_plan(
    root: str | Path = ".", *, artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR
) -> FrameworkPlan:
    root_path = Path(root).resolve()
    path = _artifact_path(root_path, artifact_dir, PLAN_FILE)
    payload = _read_json(path, label="FastAPI plan")
    plan = FrameworkPlan.from_dict(payload)
    expected = plan.with_hash().plan_hash
    if not plan.plan_hash or plan.plan_hash != expected:
        raise FrameworkMigrationError(
            "FastAPI plan hash does not match its contents; run `sanka plan --to fastapi`"
        )
    scan = load_framework_scan(root_path, artifact_dir=artifact_dir)
    if scan.scan_hash != plan.source_scan_hash:
        raise FrameworkMigrationError(
            "the scan changed after this plan was reviewed; run `sanka plan --to fastapi` again"
        )
    return plan


def apply_fastapi_plan(
    root: str | Path = ".",
    *,
    artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR,
    output: str | Path | None = None,
    plan_hash: str,
    force: bool = False,
    sql_engine: str | None = None,
) -> tuple[Path, int]:
    root_path = Path(root).resolve()
    plan = load_fastapi_plan(root_path, artifact_dir=artifact_dir)
    if not plan_hash:
        raise FrameworkMigrationError("a nonempty reviewed FastAPI plan hash is required")
    if plan_hash != plan.plan_hash:
        raise FrameworkMigrationError(
            f"reviewed plan hash {plan_hash!r} does not match current plan {plan.plan_hash!r}"
        )
    if plan.database_required:
        if sql_engine is not None and sql_engine != plan.sql_engine:
            requirement = " (psycopg also requires PostgreSQL)" if sql_engine == "psycopg" else ""
            raise FrameworkMigrationError(
                f"ORM {sql_engine!r} does not match reviewed plan {plan.sql_engine!r}; "
                f"run `sanka plan` again{requirement}"
            )
        try:
            engine = resolve_sql_engine(plan.sql_engine)
        except ValueError as error:
            raise FrameworkMigrationError(str(error)) from error
    else:
        engine = plan.sql_engine
    output_value = str(output) if output is not None else plan.default_output
    output_path = _resolve_output(root_path, output_value)
    planned_output = _resolve_output(root_path, plan.default_output)
    if output_path != planned_output:
        raise FrameworkMigrationError(
            f"output {output_path} does not match the reviewed target {planned_output}; "
            "run `sanka plan` again"
        )
    if output_path == root_path:
        raise FrameworkMigrationError("generated output cannot overwrite the source root")
    updating = plan.generation_mode == "update"
    if not updating and output_path.exists() and any(output_path.iterdir()) and not force:
        raise FrameworkMigrationError(
            f"output is not empty: {output_path}; pass --force to replace generated files"
        )
    relative_source = os.path.relpath(root_path, output_path)
    scan = load_framework_scan(root_path, artifact_dir=artifact_dir)
    layout = plan.target_generation_mode if updating else plan.generation_mode
    if updating:
        manifest = _read_json(output_path / GENERATED_MANIFEST, label="generated target manifest")
        if _target_fingerprint(output_path, manifest) != plan.target_fingerprint:
            raise FrameworkMigrationError(
                "the generated target changed after planning; run `sanka plan` again"
            )
        for operation in plan.file_operations:
            if not operation.expected_hash:
                continue
            path = output_path / operation.path
            if not path.is_file() or _text_hash(path) != operation.expected_hash:
                raise FrameworkMigrationError(
                    f"target file changed after planning: {operation.path}; run `sanka plan` again"
                )
        conflicts = [item.path for item in plan.file_operations if item.action == "conflict"]
        if conflicts and not force:
            raise FrameworkMigrationError(
                "generated target has user-modified files: "
                + ", ".join(conflicts)
                + "; review them or pass --force"
            )
        with tempfile.TemporaryDirectory(prefix="sanka-update-") as temporary:
            staged = Path(temporary)
            count = _render_fastapi_output(
                plan,
                scan,
                staged,
                layout=layout,
                source_root=relative_source,
                sql_engine=engine,
            )
            output_path.mkdir(parents=True, exist_ok=True)
            actions = {item.path: item.action for item in plan.file_operations}
            for source in sorted(path for path in staged.rglob("*") if path.is_file()):
                relative = str(source.relative_to(staged))
                if actions.get(relative) == "unchanged":
                    continue
                destination = output_path / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
    else:
        output_path.mkdir(parents=True, exist_ok=True)
        count = _render_fastapi_output(
            plan,
            scan,
            output_path,
            layout=layout,
            source_root=relative_source,
            sql_engine=engine,
        )
    return output_path, count


def _render_fastapi_output(
    plan: FrameworkPlan,
    scan: FrameworkScan,
    output: Path,
    *,
    layout: str,
    source_root: str,
    sql_engine: str,
) -> int:
    if layout not in {"full", "minimal"}:
        raise FrameworkMigrationError(f"unsupported output layout: {layout}")
    if plan.mode == NATIVE_STRATEGY:
        return _render_native_output(
            plan,
            scan,
            output,
            entrypoint="app/main.py" if layout == "full" else "app.py",
            source_root=source_root,
            sql_engine=sql_engine,
            layout=layout,
        )
    return _render_bridge_output(
        plan,
        output,
        source_root=source_root,
        layout=layout,
    )


def write_bench_candidate(
    root: str | Path = ".",
    destination: str | Path = "bench-candidate",
    *,
    artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR,
) -> Path:
    """Emit a Sanka Migration Bench candidate from the reviewed native plan.

    The overlay merges into the benchmark's copy of the source repository, so
    the entrypoint is the bench's fixed ``target_app.py`` and ``source_root``
    is the workspace root itself. The bench fixture intentionally retains
    Django for ORM access, so this projection uses its installed Django rather
    than the normal plan's independently installed async SQL engine.
    """
    root_path = Path(root).resolve()
    plan = load_fastapi_plan(root_path, artifact_dir=artifact_dir)
    if plan.mode != NATIVE_STRATEGY:
        raise FrameworkMigrationError(
            "benchmark candidates require a native plan; run `sanka plan --to fastapi`"
        )
    scan = load_framework_scan(root_path, artifact_dir=artifact_dir)
    destination_path = Path(destination)
    if not destination_path.is_absolute():
        destination_path = root_path / destination_path
    destination_path = destination_path.resolve()
    if destination_path == root_path:
        raise FrameworkMigrationError("benchmark candidate cannot overwrite the source root")
    overlay = destination_path / "overlay"
    _render_native_output(
        plan,
        scan,
        overlay,
        entrypoint="target_app.py",
        source_root=".",
        sql_engine="django",
        preserve_carryover=True,
    )
    _write_text(
        destination_path / "candidate.yaml",
        (
            "schema_version: sanka-bench/candidate/v0.1\n"
            "id: sanka-native\n"
            "kind: overlay\n"
            "overlay: overlay\n"
            "provenance:\n"
            "  producer: sanka\n"
            f"  revision: {plan.plan_hash}\n"
            "  command: sanka scan && sanka plan --to fastapi && sanka apply"
            " --plan-hash <hash> --bench-candidate <dir>\n"
        ),
    )
    # The candidate carries its own gap disclosure: the reviewed plan, a
    # human-readable gap report, and a fast structural verify. Whoever holds
    # the candidate directory sees exactly what was and was not generated
    # without digging into the source checkout's .sanka/ artifacts.
    _write_json(destination_path / "plan-fastapi.json", plan.to_dict())
    _write_text(destination_path / "GAP-REPORT.md", _render_gap_report(plan, scan))
    _write_json(destination_path / "gap-report.json", _gap_report_payload(plan, scan))
    try:
        verify_report = verify_fastapi_migration(
            root_path, artifact_dir=artifact_dir, output=overlay, probe_http=False
        )
    except FrameworkMigrationError as error:
        verify_report = {"ok": False, "error": str(error)}
    _write_json(destination_path / "verify-report.json", verify_report)
    return destination_path


def write_gap_report(
    root: str | Path = ".",
    destination: str | Path = "gap-report",
    *,
    artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR,
) -> Path:
    """Emit the unsupported-route inventory without generating an application.

    This is the low-readiness deliverable: instead of a mostly-empty scaffold
    that silently 404s, the caller gets the reviewed plan plus a structured
    checklist of every route that still needs a hand-written handler."""
    root_path = Path(root).resolve()
    plan = load_fastapi_plan(root_path, artifact_dir=artifact_dir)
    if plan.mode != NATIVE_STRATEGY:
        raise FrameworkMigrationError(
            "gap reports describe native plans; run `sanka plan --to fastapi`"
        )
    scan = load_framework_scan(root_path, artifact_dir=artifact_dir)
    destination_path = Path(destination)
    if not destination_path.is_absolute():
        destination_path = root_path / destination_path
    destination_path = destination_path.resolve()
    if destination_path == root_path:
        raise FrameworkMigrationError("gap report cannot overwrite the source root")
    if destination_path.exists() and not destination_path.is_dir():
        raise FrameworkMigrationError(
            f"gap report destination is not a directory: {destination_path}"
        )
    allowed_existing = {"GAP-REPORT.md", "gap-report.json", "plan-fastapi.json"}
    if destination_path.is_dir():
        unexpected = sorted(
            path.name for path in destination_path.iterdir() if path.name not in allowed_existing
        )
        if unexpected:
            raise FrameworkMigrationError(
                "gap report destination contains non-report files; refusing to leave a stale "
                f"scaffold in place: {destination_path} ({', '.join(unexpected)})"
            )
    destination_path.mkdir(parents=True, exist_ok=True)
    _write_json(destination_path / "plan-fastapi.json", plan.to_dict())
    _write_text(destination_path / "GAP-REPORT.md", _render_gap_report(plan, scan))
    _write_json(destination_path / "gap-report.json", _gap_report_payload(plan, scan))
    return destination_path


def _field_payload(field: SerializerFieldIR) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": field.name,
        "relation": dict(field.relation),
        "kind": field.kind,
        "coerce_to_string": field.coerce_to_string,
        "uuid_default": field.uuid_default,
        "default_on_create_only": field.default_on_create_only,
        "timezone": field.timezone,
        "required": field.required,
        "read_only": field.read_only,
        "allow_null": field.allow_null,
        "allow_blank": field.allow_blank,
        "trim_whitespace": field.trim_whitespace,
        "max_length": field.max_length,
        "min_length": field.min_length,
        "min_value": field.min_value,
        "max_value": field.max_value,
        "max_digits": field.max_digits,
        "decimal_places": field.decimal_places,
        "choices": list(field.choices),
        "unique": field.unique,
        "unique_message": field.unique_message,
        "has_default": field.has_default,
        "default": field.default,
        "attname": field.attname,
        "messages": dict(field.messages),
    }
    if field.child is not None:
        payload["child"] = {
            "model_module": field.child.model_module,
            "model_class": field.child.model_class,
            "object_name": field.child.object_name,
            "db_table": field.child.db_table,
            "pk_attname": field.child.pk_attname,
            "ordering": list(field.child.ordering),
            "fields": [_field_payload(item) for item in field.child.fields],
        }
    return payload


def _carryover_function_name(ir: SerializerIR) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", ir.name.lower()).strip("_")
    return f"create_{slug}"


def _create_payload(ir: SerializerIR, *, preserve_carryover: bool = False) -> dict[str, Any]:
    if ir.create_style == "contract":
        return dict(ir.create_contract or {"style": "unsupported"})
    if ir.create_style == "carryover":
        if preserve_carryover:
            return {"style": "carryover", "function": _carryover_function_name(ir)}
        return dict(ir.create_contract or {"style": "unsupported"})
    return {"style": "default"}


# fmt: off
_USER_LOGIC_HEADER = """\
# Generated by Sanka under the license selected for this generated application.
\"\"\"Author-owned write logic carried over verbatim from the source serializers.

Each function below is the application's own ``create()`` method, re-emitted
with its DRF exception type swapped for the native shim. The logic — including
transaction boundaries and business rules — runs unchanged against the
retained Django ORM.
\"\"\"

from __future__ import annotations


def _normalize_detail(detail):
    if isinstance(detail, str):
        return [detail]
    return detail


class ValidationError(Exception):
    def __init__(self, detail):
        self.detail = _normalize_detail(detail)
        super().__init__(self.detail)


class _SerializersShim:
    ValidationError = ValidationError


_SERIALIZERS_SHIM = _SerializersShim()
"""
# fmt: on


def _render_user_logic(resources: dict[str, dict[str, Any]], scan: FrameworkScan) -> str | None:
    serializer_by_name = {item.name: item for item in scan.serializer_details}
    sections: list[tuple[str, str]] = []
    for resource in resources.values():
        create = resource.get("create") or {}
        if create.get("style") != "carryover":
            continue
        ir = serializer_by_name[cast(str, resource["serializer"])]
        if ir.create_source is None:
            raise FrameworkMigrationError(
                f"serializer {ir.name} lost its carried create() source; run `sanka scan`"
            )
        function_name = cast(str, create["function"])
        sections.append((function_name, _transform_carryover(ir, function_name)))
    if not sections:
        return None
    body = "\n\n".join(code for _, code in sorted(sections))
    return f"{_USER_LOGIC_HEADER}\n\n{body}\n"


_USER_VIEWS_HEADER = """\
# Generated by Sanka under the license selected for this generated application.
\"\"\"Author-owned view logic carried over verbatim from the source viewsets.

Each class below subclasses the native runtime's ``CarryoverView``: ``super()``
calls reach the generated handlers and DRF's ``Response``, ``status`` and
``Request`` names are the runtime's shims. The action logic runs unchanged.
\"\"\"

from __future__ import annotations
"""


def _render_user_views(
    resources: dict[str, dict[str, Any]], scan: FrameworkScan, *, module_prefix: str
) -> str | None:
    import textwrap

    views_by_name = {item.name: item for item in scan.view_details}
    classes: list[str] = []
    imports: set[str] = set()
    aliases: dict[str, str] = {}
    shim_names = {"Request": "CarryRequest", "Response": "CarryResponse", "status": "carry_status"}
    for resource in resources.values():
        if not resource.get("view_carryover"):
            continue
        view_ir = views_by_name.get(str(resource["view"]))
        if view_ir is None or not view_ir.carryover:
            raise FrameworkMigrationError(
                f"view {resource['view']} lost its carried action source; run `sanka scan`"
            )
        carry = view_ir.carryover
        for alias, module, attr in carry.get("imports", ()):
            if module == "__sanka_view_shim__":
                aliases[str(alias)] = shim_names[str(attr)]
            elif attr is None:
                imports.add(f"import {module}" + (f" as {alias}" if alias != module else ""))
            else:
                imports.add(
                    f"from {module} import {attr}" + (f" as {alias}" if alias != attr else "")
                )
        lines = [f"class {carry['class_name']}(CarryoverView):"]
        for method in carry["methods"]:
            lines.append(textwrap.indent(str(method["source"]).rstrip("\n"), "    "))
            lines.append("")
        classes.append("\n".join(lines).rstrip("\n"))
    if not classes:
        return None
    runtime = f"{module_prefix}.sanka_native" if module_prefix else "sanka_native"
    imports.add(f"from {runtime} import CarryRequest, CarryResponse, CarryoverView, carry_status")
    alias_lines = [f"{alias} = {target}" for alias, target in sorted(aliases.items())]
    return (
        _USER_VIEWS_HEADER
        + "\n"
        + "\n".join(sorted(imports))
        + "\n\n"
        + ("\n".join(alias_lines) + "\n\n" if alias_lines else "")
        + "\n"
        + "\n\n\n".join(classes)
        + "\n"
    )


def _transform_carryover(ir: SerializerIR, function_name: str) -> str:
    tree = ast.parse(cast(str, ir.create_source))
    func = tree.body[0]
    if not isinstance(func, ast.FunctionDef):
        raise FrameworkMigrationError(f"carried create() for {ir.name} is not a function")
    func.name = function_name
    func.args.args = func.args.args[1:]
    preamble: list[ast.stmt] = []
    for alias, module, attr in ir.create_imports:
        if module == "__sanka_shim__":
            target = "_SERIALIZERS_SHIM" if attr == "serializers" else "ValidationError"
            preamble.append(
                ast.Assign(
                    targets=[ast.Name(id=alias, ctx=ast.Store())],
                    value=ast.Name(id=target, ctx=ast.Load()),
                )
            )
        elif attr is None and module == "django.db.transaction":
            preamble.append(
                ast.ImportFrom(
                    module="django.db",
                    names=[
                        ast.alias(
                            name="transaction",
                            asname=None if alias == "transaction" else alias,
                        )
                    ],
                    level=0,
                )
            )
        else:
            preamble.append(
                ast.ImportFrom(
                    module=module,
                    names=[
                        ast.alias(name=cast(str, attr), asname=None if alias == attr else alias)
                    ],
                    level=0,
                )
            )
    func.body = preamble + func.body
    ast.fix_missing_locations(tree)
    return ast.unparse(ast.Module(body=[func], type_ignores=[]))


def _allow_headers(generated: list[PlannedRoute]) -> dict[str, str]:
    """Per-path Allow header values matching DRF's method advertisement."""
    methods_by_path: dict[str, set[str]] = {}
    for route in generated:
        methods_by_path.setdefault(route.path, set()).add(route.method.upper())
    order = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
    allow: dict[str, str] = {}
    for path, methods in methods_by_path.items():
        if "GET" in methods:
            methods.add("HEAD")
        methods.add("OPTIONS")
        allow[path] = ", ".join(method for method in order if method in methods)
    return allow


def _render_bridge_output(
    plan: FrameworkPlan,
    output_path: Path,
    *,
    source_root: str,
    layout: str = "minimal",
) -> int:
    automatic = [route for route in plan.routes if route.automatic]
    full = layout == "full"
    runtime_dir = output_path / "app" / "generated" if full else output_path
    entrypoint = "app/main.py" if full else "app.py"
    compat_path = "app/generated/sanka_compat.py" if full else "sanka_compat.py"
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "generator": "sanka",
        "mode": plan.mode,
        "source_scan_hash": plan.source_scan_hash,
        "plan_hash": plan.plan_hash,
        "settings_module": plan.settings_module,
        "entrypoint": entrypoint,
        "generation_mode": layout,
        "package_manager": plan.package_manager,
        "swagger_ui": plan.swagger_ui,
        "database_required": False,
        "generated_files": [entrypoint, compat_path],
        "routes": [
            {
                "method": route.method,
                "path": route.path,
                "operation": route.operation,
                "source_view": route.source_view,
                "strategy": route.strategy,
            }
            for route in automatic
        ],
        "source_root": source_root,
    }
    module = "app.generated.sanka_compat" if full else "sanka_compat"
    app_source = _render_app(module)
    if full:
        app_source += _render_full_app_setup()
    _write_text(output_path / entrypoint, app_source)
    _write_text(runtime_dir / "sanka_compat.py", _render_compatibility_runtime())
    _write_text(
        output_path / "README.md",
        _render_generated_readme(plan, entrypoint=entrypoint),
    )
    requirements = "fastapi>=0.115,<1\nuvicorn[standard]>=0.30,<1\n"
    _write_text(output_path / "requirements.txt", requirements)
    _write_text(output_path / "pyproject.toml", render_generated_pyproject(requirements))
    _write_text(output_path / "requirements-test.txt", "httpx>=0.27,<1\nhttpx2>=2,<3\n")
    if full:
        manifest["generated_files"].extend(
            _render_full_support(output_path, database_required=False)
        )
    _finalize_generated_manifest(output_path, runtime_dir, manifest)
    return len(automatic)


def _render_native_output(
    plan: FrameworkPlan,
    scan: FrameworkScan,
    output_path: Path,
    *,
    entrypoint: str,
    source_root: str,
    sql_engine: str,
    preserve_carryover: bool = False,
    layout: str = "minimal",
) -> int:
    generated = [
        route
        for route in plan.routes
        if route.strategy in (ROUTE_STRATEGY_NATIVE_CRUD, ROUTE_STRATEGY_NATIVE_API_ROOT)
    ]
    if not generated:
        raise FrameworkMigrationError(
            "the native plan contains no generatable routes; nothing to apply"
        )
    dropped = [route for route in plan.routes if route.strategy == ROUTE_STRATEGY_DROPPED_ALIAS]
    manual = sorted(
        (route for route in plan.routes if route.strategy == ROUTE_STRATEGY_MANUAL),
        key=lambda route: (route.path, route.method),
    )
    scan_routes = {route.key: route for route in scan.routes}
    serializer_by_name = {item.name: item for item in scan.serializer_details}
    views_by_name = {item.name: item for item in scan.view_details}
    resources: dict[str, dict[str, Any]] = {}
    api_root_paths: set[str] = set()
    for planned in sorted(generated, key=lambda route: (route.path, route.method)):
        if planned.strategy == ROUTE_STRATEGY_NATIVE_API_ROOT:
            api_root_paths.add(planned.path)
            continue
        route = scan_routes[planned.key]
        if route.serializer is None or route.serializer not in serializer_by_name:
            raise FrameworkMigrationError(
                f"native route {planned.key} has no captured serializer; run `sanka scan`"
            )
        ir = serializer_by_name[route.serializer]
        view_ir = views_by_name.get(route.view)
        auth_payload: dict[str, Any] | None = None
        if view_ir is not None and view_ir.auth is not None and view_ir.auth.token_keyword:
            auth = view_ir.auth
            auth_payload = {
                "require_authenticated": auth.require_authenticated,
                "user": auth.user,
                "token_keyword": auth.token_keyword,
                "token_db_table": auth.token_db_table,
                "token_key_column": auth.token_key_column,
                "token_key_max_length": auth.token_key_max_length,
                "token_user_column": auth.token_user_column,
                "owner_attname": auth.owner_attname,
                "inject_owner_attname": auth.inject_owner_attname,
                "messages": dict(auth.messages),
            }
        resource = resources.setdefault(
            route.view,
            {
                "view": route.view,
                "auth": auth_payload,
                "access": dict(view_ir.access) if view_ir else {},
                "serializer": ir.name,
                "model_module": ir.model_module,
                "model_class": ir.model_class,
                "object_name": ir.object_name,
                "db_table": ir.db_table,
                "pk_attname": ir.pk_attname,
                "ordering": list(ir.ordering),
                "lookup": ir.lookup,
                "lookup_url_kwarg": view_ir.lookup_url_kwarg if view_ir else None,
                "lookup_regex": view_ir.lookup_regex if view_ir is not None else None,
                "listing": dict(view_ir.listing) if view_ir is not None else {},
                "view_carryover": (
                    {
                        "class": str(view_ir.carryover["class_name"]),
                        "operations": list(view_ir.carryover["operations"]),
                    }
                    if view_ir is not None and view_ir.carryover
                    else None
                ),
                "fields": [_field_payload(field) for field in ir.fields],
                "storage": list(ir.storage),
                "create": _create_payload(ir, preserve_carryover=preserve_carryover),
                "update_drops": None if ir.update_drops is None else list(ir.update_drops),
                "routes": [],
            },
        )
        resource["routes"].append(
            {"method": planned.method, "path": planned.path, "operation": planned.operation}
        )
    options_by_path: dict[str, Any] = {}
    for planned in generated:
        scanned = scan_routes.get(planned.key)
        if scanned is not None and scanned.options and planned.path not in options_by_path:
            options_by_path[planned.path] = dict(scanned.options)
    full = layout == "full"
    runtime_dir = output_path / "app" / "generated" if full else output_path
    module_prefix = "app.generated" if full else ""
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "generator": "sanka",
        "mode": plan.mode,
        "source_scan_hash": plan.source_scan_hash,
        "plan_hash": plan.plan_hash,
        "settings_module": plan.settings_module,
        "sql_engine": sql_engine,
        "generation_mode": layout,
        "package_manager": plan.package_manager,
        "swagger_ui": plan.swagger_ui,
        "database_required": plan.database_required,
        "database": {
            "vendor": scan.database.vendor,
            "name": scan.database.name,
            "host": scan.database.host,
            "port": scan.database.port,
            "user": scan.database.user,
        },
        "entrypoint": entrypoint,
        "allow": _allow_headers(
            [*generated, *(route for route in manual if _stub_safe_path(route.path))]
        ),
        "options": options_by_path,
        "generic_messages": dict(scan.generic_messages),
        "status_codes": dict(scan.status_codes),
        "http_security": dict(scan.http_security),
        "generated_files": [entrypoint],
        "resources": [resources[name] for name in sorted(resources)],
        "api_roots": [
            {"path": root.path, "links": [list(link) for link in root.links]}
            for root in scan.api_roots
            if root.path in api_root_paths
        ],
        "routes": [
            {
                "method": route.method,
                "path": route.path,
                "operation": route.operation,
                "source_view": route.source_view,
                "strategy": route.strategy,
            }
            for route in sorted(generated, key=lambda route: (route.path, route.method))
        ],
        "dropped_routes": [
            {"method": route.method, "path": route.path, "reason": "format-suffix alias"}
            for route in sorted(dropped, key=lambda route: (route.path, route.method))
        ],
        # The gap inventory travels WITH the overlay: whoever holds the
        # generated app also holds the machine-readable list of everything it
        # does not cover, instead of that truth living only in .sanka/ files
        # that never leave the source checkout.
        "readiness": plan.readiness,
        "native_eligible_routes": plan.native_eligible_routes,
        "needs_adaptation_routes": plan.needs_adaptation_routes,
        "unsupported_routes": [
            {
                "method": route.method,
                "path": route.path,
                "operation": route.operation,
                "source_view": route.source_view,
                "reasons": [
                    {"code": reason.code, "feature": reason.feature, "message": reason.message}
                    for reason in route.adaptation_reasons
                ],
                "parity_notes": [_parity_note_payload(note) for note in route.parity_notes],
                "stubbed": _stub_safe_path(route.path),
            }
            for route in manual
        ],
        "skipped_routes": [
            {"pattern": item.pattern, "view": item.view, "reason": item.reason}
            for item in scan.skipped_routes
        ],
        "source_root": source_root,
    }

    def write_generated(name: str, text: str) -> None:
        destination = output_path if name in {"requirements.txt", "pyproject.toml"} else runtime_dir
        _write_text(destination / name, text)

    try:
        generated_names = render_async_sql_files(
            write_generated,
            entrypoint=entrypoint,
            manifest=manifest,
            sql_engine=sql_engine,
            database_required=plan.database_required,
            module_prefix=module_prefix,
        )
    except ValueError as error:
        raise FrameworkMigrationError(str(error)) from error
    user_logic = _render_user_logic(resources, scan) if preserve_carryover else None
    if user_logic is not None:
        manifest["has_user_logic"] = True
        generated_names.append("sanka_user_logic.py")
        _write_text(runtime_dir / "sanka_user_logic.py", user_logic)
    user_views = _render_user_views(resources, scan, module_prefix=module_prefix)
    if user_views is not None:
        manifest["has_user_views"] = True
        generated_names.append("sanka_user_views.py")
        _write_text(runtime_dir / "sanka_user_views.py", user_views)
    generated_paths = [f"app/generated/{name}" if full else name for name in generated_names]
    manifest["generated_files"] = [entrypoint, *generated_paths]
    app_source = _render_native_app(manifest, module_prefix=module_prefix)
    if full:
        app_source += _render_full_app_setup()
    _write_text(output_path / entrypoint, app_source)
    _write_text(
        output_path / "README.md",
        _render_native_readme(plan, sql_engine, entrypoint=entrypoint),
    )
    _write_text(output_path / "requirements-test.txt", "httpx>=0.27,<1\nhttpx2>=2,<3\n")
    if full:
        manifest["generated_files"].extend(
            _render_full_support(output_path, database_required=plan.database_required)
        )
    _finalize_generated_manifest(output_path, runtime_dir, manifest)
    return len(generated)


def _render_full_support(output: Path, *, database_required: bool) -> list[str]:
    package = "# Generated by Sanka.\n"
    files = {
        "app/__init__.py": package,
        "app/api/__init__.py": package,
        "app/api/health.py": _FULL_HEALTH,
        "app/api/router.py": _FULL_ROUTER,
        "app/core/__init__.py": package,
        "app/core/config.py": _FULL_CONFIG,
        "app/core/logging.py": _FULL_LOGGING,
        "app/generated/__init__.py": package,
        "tests/__init__.py": package,
    }
    if database_required:
        files["app/core/database.py"] = _FULL_DATABASE
    for name, source in files.items():
        _write_text(output / name, source)
    env = "APP_NAME=Sanka FastAPI\nAPP_ENV=development\nLOG_LEVEL=INFO\nLOG_FORMAT=console\n"
    if database_required:
        env += "SANKA_DATABASE_URL=<set-me>\n"
    _write_text(output / ".env.example", env)
    _write_text(output / ".gitignore", ".env\n.venv/\n__pycache__/\n.pytest_cache/\n")
    return sorted(name for name in files if name.endswith(".py"))


def _render_full_app_setup() -> str:
    return """

from app.api.router import router as api_router
from app.core.logging import RequestContextMiddleware, configure_logging

configure_logging()
app.add_middleware(RequestContextMiddleware)
app.include_router(api_router)
"""


def _finalize_generated_manifest(output: Path, runtime_dir: Path, manifest: dict[str, Any]) -> None:
    project_manifest = output / PROJECT_MANIFEST
    manifest_paths = {
        output / GENERATED_MANIFEST,
        runtime_dir / GENERATED_MANIFEST,
        project_manifest,
    }
    owned = set(manifest.get("generated_files") or ())
    owned.update(
        {
            ".env.example",
            ".gitignore",
            "README.md",
            "pyproject.toml",
            "requirements.txt",
            "requirements-test.txt",
        }
    )
    hashes = {
        name: _text_hash(output / name)
        for name in sorted(owned)
        if (output / name).is_file() and (output / name) not in manifest_paths
    }
    manifest["generated_file_hashes"] = hashes
    _write_json(output / GENERATED_MANIFEST, manifest)
    if runtime_dir != output:
        _write_json(runtime_dir / GENERATED_MANIFEST, manifest)
        _write_json(project_manifest, manifest)


_FULL_CONFIG = """# Generated by Sanka.
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    app_name: str = os.environ.get("APP_NAME", "Sanka FastAPI")
    app_env: str = os.environ.get("APP_ENV", "development")
    log_level: str = os.environ.get("LOG_LEVEL", "INFO")
    log_format: str = os.environ.get("LOG_FORMAT", "console")


settings = Settings()
"""


_FULL_HEALTH = """# Generated by Sanka.
from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
"""


_FULL_ROUTER = """# Generated by Sanka.
from fastapi import APIRouter

from app.api.health import router as health_router

router = APIRouter()
router.include_router(health_router)
"""


_FULL_DATABASE = """# Generated by Sanka.
from app.generated.sanka_store import close_db, init_db

__all__ = ["close_db", "init_db"]
"""


_FULL_LOGGING = """# Generated by Sanka.
from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings

request_id = contextvars.ContextVar("request_id", default="")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "level": record.levelname,
                "message": record.getMessage(),
                "request_id": request_id.get(),
            },
            ensure_ascii=False,
        )


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter()
        if settings.log_format == "json"
        else logging.Formatter("%(levelname)s %(message)s")
    )
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        handlers=[handler],
        force=True,
    )


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        value = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id.set(value)
        started = time.perf_counter()
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = value
            logging.getLogger("http").info(
                "%s %s %s %.2fms",
                request.method,
                request.url.path,
                response.status_code,
                (time.perf_counter() - started) * 1000,
            )
            return response
        finally:
            request_id.reset(token)
"""


def verify_fastapi_migration(
    root: str | Path = ".",
    *,
    artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR,
    output: str | Path | None = None,
    probe_http: bool = True,
    cases: str | Path | None = None,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    scan = load_framework_scan(root_path, artifact_dir=artifact_dir)
    plan = load_fastapi_plan(root_path, artifact_dir=artifact_dir)
    output_value = str(output) if output is not None else plan.default_output
    output_path = Path(output_value)
    if not output_path.is_absolute():
        output_path = root_path / output_path
    output_path = output_path.resolve()
    scan_path = _artifact_path(root_path, artifact_dir, SCAN_FILE).resolve()
    plan_path = _artifact_path(root_path, artifact_dir, PLAN_FILE).resolve()
    manifest_path = (output_path / GENERATED_MANIFEST).resolve()
    manifest = _read_json(manifest_path, label="generated manifest")
    if manifest.get("source_scan_hash") != scan.scan_hash:
        raise FrameworkMigrationError("generated output does not match the current scan")
    if manifest.get("plan_hash") != plan.plan_hash:
        raise FrameworkMigrationError("generated output does not match the reviewed plan")
    expected = {
        route.key
        for route in plan.routes
        if route.automatic and route.strategy != ROUTE_STRATEGY_DROPPED_ALIAS
    }
    needs_adaptation = sorted(route.key for route in plan.routes if not route.automatic)
    dropped = sorted(
        route.key for route in plan.routes if route.strategy == ROUTE_STRATEGY_DROPPED_ALIAS
    )
    actual = {
        f"{str(route['method']).upper()} {route['path']}" for route in manifest.get("routes", [])
    }
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    _compile_generated_files(output_path, manifest)
    probes: list[dict[str, Any]] = []
    generated_environment: GeneratedEnvironment | None = None
    if probe_http and not missing and not extra:
        if plan.mode == NATIVE_STRATEGY:
            generated_environment = ensure_generated_environment(output_path)
        probes = _probe_read_only_routes(
            root_path,
            output_path,
            manifest,
            cases=_load_verification_cases(root_path, cases),
            target_python=(
                generated_environment.python if generated_environment is not None else None
            ),
        )
    failed_probes = [probe for probe in probes if not probe["ok"]]
    generated_files = [
        str((output_path / str(name)).resolve())
        for name in manifest.get("generated_files", [])
        if str(name).endswith(".py")
    ]
    return {
        "ok": not missing and not extra and not failed_probes and not needs_adaptation,
        "mode": plan.mode,
        "routes": {
            "scanned": len(scan.routes),
            "planned": len(plan.routes),
            "generated": len(actual),
            "missing": missing,
            "extra": extra,
            "needs_adaptation": needs_adaptation,
            "dropped": dropped,
        },
        "http": {
            "enabled": probe_http,
            "safe_routes": len(
                [
                    route
                    for route in manifest.get("routes", [])
                    if route.get("method") in {"GET", "HEAD"} and "{" not in route.get("path", "")
                ]
            ),
            "probed": len(probes),
            "passed": len(probes) - len(failed_probes),
            "failed": failed_probes,
        },
        "scan_hash": scan.scan_hash,
        "plan_hash": plan.plan_hash,
        "output": str(output_path),
        "paths": {
            "source": str(root_path),
            "scan": str(scan_path),
            "plan": str(plan_path),
            "generated": str(output_path),
            "manifest": str(manifest_path),
            "pyproject": str((output_path / "pyproject.toml").resolve()),
            "environment": (
                str(generated_environment.root) if generated_environment is not None else None
            ),
            "python": (
                str(generated_environment.python) if generated_environment is not None else None
            ),
            "lockfile": (
                str(generated_environment.lockfile)
                if generated_environment is not None and generated_environment.lockfile is not None
                else None
            ),
        },
        "generated_files": generated_files,
    }


def _stub_safe_path(path: str) -> bool:
    """True when an unsupported route's path can still be mounted for a stub.

    Paths that keep regex metacharacters after conversion cannot be expressed
    as a FastAPI path; those routes stay absent and are disclosed as such."""
    return re.search(r"[\[\]()+*?|\\^$]", path) is None


def _parity_note_payload(note: ParityNote) -> dict[str, Any]:
    return {
        "family": note.family,
        "code": note.code,
        "message": note.message,
        "source": note.source,
    }


def _compile_generated_files(output: Path, manifest: dict[str, Any]) -> None:
    names = [
        str(name)
        for name in manifest.get("generated_files", ["app.py", "sanka_compat.py"])
        if str(name).endswith(".py")
    ]
    for name in names:
        path = output / name
        if not path.is_file():
            raise FrameworkMigrationError(f"generated file is missing: {path}")
        text = path.read_text(encoding="utf-8")
        try:
            compile(text, str(path), "exec")
        except SyntaxError as error:
            raise FrameworkMigrationError(
                f"generated Python is invalid: {path}: {error}"
            ) from error
        if manifest.get("mode") == NATIVE_STRATEGY:
            if manifest.get("sql_engine") == "django":
                # The retained-ORM projection imports Django by design; what
                # must never appear is the request-serving machinery.
                serving_machinery = (
                    "django.core.asgi",
                    "django.core.wsgi",
                    "django.core.handlers",
                    "django.test",
                )
                if any(item in text for item in serving_machinery):
                    raise FrameworkMigrationError(
                        f"native output imports Django serving machinery: {path}"
                    )
            elif "django.setup" in text or "import django" in text:
                raise FrameworkMigrationError(f"native output still imports Django: {path}")


def _load_generated_app(output: Path) -> Any:
    manifest = _read_json(output / GENERATED_MANIFEST, label="generated manifest")
    entrypoint = output / str(manifest.get("entrypoint") or "app.py")
    module_name = f"_sanka_generated_{abs(hash(output))}"
    spec = importlib.util.spec_from_file_location(module_name, entrypoint)
    if spec is None or spec.loader is None:
        raise FrameworkMigrationError("could not load the generated FastAPI application")
    if str(output) not in sys.path:
        sys.path.insert(0, str(output))
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ModuleNotFoundError as error:
        raise FrameworkMigrationError(
            f"generated app is missing a dependency ({error.name}); "
            f"install {output / 'requirements.txt'}"
        ) from error
    return module.app


def _bind_source_database() -> None:
    django_conf = importlib.import_module("django.conf")
    name = django_conf.settings.DATABASES.get("default", {}).get("NAME")
    if name and not os.environ.get("SANKA_DATABASE_URL") and not os.environ.get("SANKA_TEST_DB"):
        os.environ["SANKA_TEST_DB"] = str(name)
    connections = importlib.import_module("django.db").connections
    connections.close_all()


def _probe_read_only_routes(
    root: Path,
    output: Path,
    manifest: dict[str, Any],
    *,
    cases: list[dict[str, Any]],
    target_python: Path | None,
) -> list[dict[str, Any]]:
    _bootstrap_django(root, str(manifest["settings_module"]))
    _bind_source_database()
    django_test = importlib.import_module("django.test")
    source = django_test.Client()
    automatic = [
        {"method": route.get("method"), "path": route.get("path"), "headers": {}}
        for route in manifest.get("routes", [])
        if route.get("method") in {"GET", "HEAD"} and "{" not in route.get("path", "")
    ]
    probes = _deduplicate_probes(automatic + cases)
    source_responses = [_source_probe_response(source, case) for case in probes]
    if target_python is not None:
        target_responses = _native_target_probe_responses(output, target_python, probes)
    else:
        target_responses = _compatibility_target_probe_responses(output, probes)
    results: list[dict[str, Any]] = []
    for case, source_response, target_response in zip(
        probes, source_responses, target_responses, strict=True
    ):
        source_body = source_response["body"]
        target_body = target_response["body"]
        source_type = source_response["content_type"]
        target_type = target_response["content_type"]
        bodies_match = _response_bodies_match(source_body, target_body, source_type, target_type)
        compared_headers = ("allow", "location", "www-authenticate")
        headers_match = all(
            source_response["headers"].get(header, "") == target_response["headers"].get(header, "")
            for header in compared_headers
        )
        ok = (
            source_response["status"] == target_response["status"]
            and source_type == target_type
            and bodies_match
            and headers_match
        )
        results.append(
            {
                "method": case["method"],
                "path": case["path"],
                "ok": ok,
                "source_status": source_response["status"],
                "target_status": target_response["status"],
                "source_content_type": source_type,
                "target_content_type": target_type,
                "headers_match": headers_match,
            }
        )
    return results


def _deduplicate_probes(probes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for case in probes:
        method = str(case.get("method", "GET")).upper()
        path = str(case.get("path", ""))
        headers = {str(key): str(value) for key, value in dict(case.get("headers", {})).items()}
        identity = (method, path, json.dumps(headers, sort_keys=True))
        if identity in seen:
            continue
        seen.add(identity)
        unique.append({"method": method, "path": path, "headers": headers})
    return unique


def _source_probe_response(source: Any, case: dict[str, Any]) -> dict[str, Any]:
    headers = case["headers"]
    django_headers = {
        "HTTP_" + key.upper().replace("-", "_"): value
        for key, value in headers.items()
        if key.lower() not in {"content-type", "content-length"}
    }
    response = source.generic(case["method"], case["path"], **django_headers)
    importlib.import_module("django.db").connections.close_all()
    return {
        "status": response.status_code,
        "content_type": str(response.get("Content-Type", "")).split(";", 1)[0],
        "body": bytes(response.content),
        "headers": {
            name: str(response.get(name, "")) for name in ("allow", "location", "www-authenticate")
        },
    }


def _compatibility_target_probe_responses(
    output: Path, probes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    try:
        fastapi_testclient = importlib.import_module("fastapi.testclient")
    except ModuleNotFoundError as error:
        raise FrameworkMigrationError(
            "FastAPI is required to verify a compatibility bridge; install the generated "
            f"dependencies from {output / 'requirements.txt'} into the source environment"
        ) from error
    responses: list[dict[str, Any]] = []
    with fastapi_testclient.TestClient(_load_generated_app(output)) as target:
        for case in probes:
            response = target.request(case["method"], case["path"], headers=case["headers"])
            responses.append(_target_response_payload(response))
    return responses


def _target_response_payload(response: Any) -> dict[str, Any]:
    return {
        "status": response.status_code,
        "content_type": str(response.headers.get("content-type", "")).split(";", 1)[0],
        "body": response.content,
        "headers": {
            name: str(response.headers.get(name, ""))
            for name in ("allow", "location", "www-authenticate")
        },
    }


def _native_target_probe_responses(
    output: Path,
    target_python: Path,
    probes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result = subprocess.run(
        [str(target_python), "-c", _TARGET_PROBE_SCRIPT],
        cwd=output,
        env=dict(os.environ),
        input=json.dumps(probes),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "generated target probe failed").strip()
        raise FrameworkMigrationError(
            f"could not verify the generated app with {target_python}:\n{detail}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise FrameworkMigrationError(
            "generated target verification returned invalid output"
        ) from error
    if not isinstance(payload, list) or len(payload) != len(probes):
        raise FrameworkMigrationError("generated target verification returned incomplete results")
    responses: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise FrameworkMigrationError("generated target verification returned invalid results")
        try:
            responses.append(
                {
                    "status": int(item["status"]),
                    "content_type": str(item["content_type"]),
                    "body": base64.b64decode(str(item["body"]), validate=True),
                    "headers": dict(item["headers"]),
                }
            )
        except (KeyError, TypeError, ValueError) as error:
            raise FrameworkMigrationError(
                "generated target verification returned invalid response data"
            ) from error
    return responses


_TARGET_PROBE_SCRIPT = r"""import base64
import contextlib
import importlib
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

with contextlib.redirect_stdout(sys.stderr):
    manifest = json.loads(Path("sanka-manifest.json").read_text(encoding="utf-8"))
    module = Path(manifest.get("entrypoint", "app.py")).with_suffix("").as_posix().replace("/", ".")
    app = importlib.import_module(module).app

    probes = json.load(sys.stdin)
    results = []
    with TestClient(app) as client:
        for case in probes:
            response = client.request(case["method"], case["path"], headers=case["headers"])
            results.append({
                "status": response.status_code,
                "content_type": response.headers.get("content-type", "").split(";", 1)[0],
                "body": base64.b64encode(response.content).decode("ascii"),
                "headers": {
                    name: response.headers.get(name, "")
                    for name in ("allow", "location", "www-authenticate")
                },
            })
json.dump(results, sys.stdout)
"""


def _load_verification_cases(root: Path, value: str | Path | None) -> list[dict[str, Any]]:
    path = Path(value) if value is not None else root / DEFAULT_ARTIFACT_DIR / "verify-cases.json"
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        return []
    payload = _read_json(path, label="verification cases")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list):
        raise FrameworkMigrationError("verification cases must contain a `cases` array")
    cases: list[dict[str, Any]] = []
    for index, item in enumerate(raw_cases):
        if not isinstance(item, dict):
            raise FrameworkMigrationError(f"verification case {index} must be an object")
        method = str(item.get("method", "GET")).upper()
        path_value = str(item.get("path", ""))
        if method not in {"GET", "HEAD", "OPTIONS"}:
            raise FrameworkMigrationError(
                f"verification case {index} uses mutating method {method};"
                " automatic differential verification is read-only"
            )
        if not path_value.startswith("/") or "{" in path_value:
            raise FrameworkMigrationError(
                f"verification case {index} must use a concrete absolute path"
            )
        headers = item.get("headers", {})
        if not isinstance(headers, dict):
            raise FrameworkMigrationError(f"verification case {index} headers must be an object")
        cases.append({"method": method, "path": path_value, "headers": headers})
    return cases


def _response_bodies_match(
    source: bytes, target: bytes, source_type: str, target_type: str
) -> bool:
    if source_type == target_type == "application/json":
        try:
            return bool(json.loads(source) == json.loads(target))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return source == target
    return source == target


def _render_app(module: str = "sanka_compat") -> str:
    return f"""# Generated by Sanka. Replace bridge routes with native FastAPI handlers.
from {module} import create_app

app = create_app()
"""


_FASTAPI_DECORATOR = {
    "GET": "get",
    "POST": "post",
    "PUT": "put",
    "PATCH": "patch",
    "DELETE": "delete",
}

_OPERATION_FUNCS = {
    "list": "list",
    "create": "create",
    "retrieve": "get",
    "update": "replace",
    "partial_update": "update",
    "destroy": "delete",
}


def _python_ident(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "item"


def _py_str(value: str) -> str:
    return json.dumps(value)


def _unique_ident(base: str, used: set[str]) -> str:
    name = base
    index = 2
    while name in used:
        name = f"{base}_{index}"
        index += 1
    used.add(name)
    return name


def _render_native_app(manifest: dict[str, Any], *, module_prefix: str = "") -> str:
    """Emit decorator-style async FastAPI routes that call the shared native helpers."""
    docs_option = ", docs_url=None" if manifest.get("swagger_ui") is False else ""
    native_import = (
        f"from {module_prefix} import sanka_native as native"
        if module_prefix
        else "import sanka_native as native"
    )
    lines = [
        (
            "# Generated by Sanka. Async FastAPI over the existing SQL tables."
            if manifest.get("database_required", True)
            else "# Generated by Sanka. Async FastAPI application."
        ),
        "from contextlib import asynccontextmanager"
        if manifest.get("database_required", True)
        else "",
        "",
        "from fastapi import FastAPI, Request",
        "from fastapi.responses import HTMLResponse, Response",
        "from starlette.convertors import Convertor, register_url_convertor",
        "from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware",
        "from starlette.middleware.trustedhost import TrustedHostMiddleware",
        "",
        native_import,
        "_DJANGO_DEFAULT_404 = "
        + _py_str(
            '\n<!doctype html>\n<html lang="en">\n<head>\n'
            "  <title>Not Found</title>\n</head>\n<body>\n"
            "  <h1>Not Found</h1><p>The requested resource was not found on this server.</p>\n"
            "</body>\n</html>\n"
        ),
        "",
        "# FastAPI would answer a slash-less path with 307; Django never does. Without",
        "# CommonMiddleware it serves its default 404 page, with APPEND_SLASH a 301 to the",
        "# slashed route. redirect_slashes is off above so these handlers decide. The",
        "# catch-all route below keeps every unmatched path and disallowed method inside a",
        "# workspace-owned APIRoute, which is what native-serving evidence requires.",
        "@app.exception_handler(404)",
        "async def django_default_404(request: Request, _error: Exception) -> Response:",
        "    return native.not_found_response(request, _DJANGO_DEFAULT_404)",
        "",
        "# DRF answers an unsupported method with its own detail string and the Allow",
        "# header in http_method_names order; FastAPI's default differs on both.",
        "@app.exception_handler(405)",
        "async def django_rest_405(request: Request, _error: Exception) -> Response:",
        "    return await native.method_not_allowed(request)",
        "",
        '_HTTP_SECURITY = native.MANIFEST.get("http_security", {})',
        '_ALLOWED_HOSTS = _HTTP_SECURITY.get("allowed_hosts", [])',
        'if _HTTP_SECURITY.get("ssl_redirect"):',
        "    app.add_middleware(HTTPSRedirectMiddleware)",
        'if _ALLOWED_HOSTS and "*" not in _ALLOWED_HOSTS:',
        "    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_ALLOWED_HOSTS)",
        "",
        '@app.middleware("http")',
        "async def add_security_headers(request: Request, call_next):",
        "    response = await call_next(request)",
        "    native.apply_security_headers(response, request, _HTTP_SECURITY)",
        "    return response",
        "",
    ]
    if manifest.get("database_required", True):
        store_import = (
            "from app.core import database as store"
            if module_prefix
            else "import sanka_store as store"
        )
        lines[10:10] = [
            store_import,
            "",
            "",
            "@asynccontextmanager",
            "async def lifespan(_app: FastAPI):",
            "    await store.init_db()",
            "    try:",
            "        yield",
            "    finally:",
            "        await store.close_db()",
            "",
            "",
            "app = FastAPI(",
            '    title="Sanka native FastAPI application",',
            "    lifespan=lifespan,",
            "    redirect_slashes=False,",
            *(["    docs_url=None,"] if docs_option else []),
            ")",
            "",
        ]
    else:
        lines[10:10] = [
            "",
            "",
            'app = FastAPI(title="Sanka native FastAPI application", '
            f"redirect_slashes=False{docs_option})",
            "",
        ]
    used_vars: set[str] = set()
    used_funcs: set[str] = set()
    used_converters: set[str] = set()
    resource_var: dict[str, str] = {}
    object_names: dict[str, str] = {}
    lookup_by_view: dict[str, str] = {}
    converter_by_view: dict[str, dict[str, str]] = {}
    for resource in manifest["resources"]:
        view = str(resource["view"])
        ident = _python_ident(str(resource["object_name"]))
        var = _unique_ident(f"_{ident.upper()}", used_vars)
        resource_var[view] = var
        object_names[view] = ident
        lookup_by_view[view] = str(
            resource.get("lookup_url_kwarg") or resource.get("lookup") or "pk"
        )
        lookup_regex = resource.get("lookup_regex")
        patterns = dict((resource.get("access") or {}).get("parameter_regexes") or {})
        if isinstance(lookup_regex, str) and lookup_regex:
            patterns[lookup_by_view[view]] = lookup_regex
        for parameter, pattern_regex in patterns.items():
            converter = _unique_ident(f"sanka_{ident}_lookup", used_converters)
            converter_class = _unique_ident(
                f"_Sanka{ident.title()}LookupConvertor", used_converters
            )
            converter_by_view.setdefault(view, {})[parameter] = converter
            lines.extend(
                [
                    f"class {converter_class}(Convertor):",
                    f"    regex = {_py_str(pattern_regex)}",
                    "",
                    "    def convert(self, value: str) -> str:",
                    "        return value",
                    "",
                    "    def to_string(self, value: str) -> str:",
                    "        return value",
                    "",
                    "",
                    f"register_url_convertor({_py_str(converter)}, {converter_class}())",
                    "",
                ]
            )
        lines.append(f"{var} = native.resource({_py_str(view)})")
    if resource_var:
        lines.append("")
    explicit_head_paths = {
        str(entry["path"])
        for entry in [*manifest["routes"], *manifest.get("unsupported_routes", [])]
        if str(entry["method"]).upper() == "HEAD"
    }
    for route in manifest["routes"]:
        method = str(route["method"]).upper()
        path = str(route["path"])
        operation = str(route["operation"])
        decorator = _FASTAPI_DECORATOR.get(method)
        if decorator is None:
            raise FrameworkMigrationError(f"unsupported native HTTP method: {method}")
        if str(route.get("strategy")) == ROUTE_STRATEGY_NATIVE_API_ROOT:
            func = _unique_ident("api_root", used_funcs)
            if method == "GET" and path not in explicit_head_paths:
                lines.append(f"@app.head({_py_str(path)}, include_in_schema=False)")
            lines.extend(
                [
                    f"@app.{decorator}({_py_str(path)})",
                    f"async def {func}(request: Request) -> Response:",
                    f"    return await native.api_root(request, {_py_str(path)})",
                    "",
                ]
            )
            continue
        view = str(route["source_view"])
        runtime_path = path
        for parameter, converter in converter_by_view.get(view, {}).items():
            runtime_path = runtime_path.replace(f"{{{parameter}}}", f"{{{parameter}:{converter}}}")
        var = resource_var[view]
        func = _unique_ident(
            f"{_OPERATION_FUNCS.get(operation, operation)}_{object_names[view]}",
            used_funcs,
        )
        path_parameters = re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", path)
        signature = ", ".join(["request: Request", *(f"{name}: str" for name in path_parameters)])
        call = f"    return await native.handle({var}, {_py_str(operation)}, request)"
        if method == "GET" and path not in explicit_head_paths:
            lines.append(f"@app.head({_py_str(runtime_path)}, include_in_schema=False)")
        lines.extend(
            [
                f"@app.{decorator}({_py_str(runtime_path)})",
                f"async def {func}({signature}) -> Response:",
                call,
                "",
            ]
        )
    options_paths = manifest.get("options") or {}
    if options_paths:
        lines.extend(
            [
                "# OPTIONS answers DRF's SimpleMetadata body captured at scan time; the",
                "# runtime picks the anonymous or authorized variant the caller earns.",
                "",
            ]
        )
        path_views = {
            str(route["path"]): str(route.get("source_view") or "") for route in manifest["routes"]
        }
        for path in sorted(options_paths):
            view = path_views.get(path, "")
            runtime_path = path
            for parameter, converter in converter_by_view.get(view, {}).items():
                runtime_path = runtime_path.replace(
                    f"{{{parameter}}}", f"{{{parameter}:{converter}}}"
                )
            func = _unique_ident("sanka_options", used_funcs)
            path_parameters = re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", path)
            signature = ", ".join(
                ["request: Request", *(f"{name}: str" for name in path_parameters)]
            )
            lines.extend(
                [
                    f"@app.options({_py_str(runtime_path)})",
                    f"async def {func}({signature}) -> Response:",
                    f"    return await native.options_response(request, {_py_str(path)})",
                    "",
                ]
            )
    stubbed = [entry for entry in manifest.get("unsupported_routes", []) if entry.get("stubbed")]
    if stubbed:
        lines.extend(
            [
                "# Routes outside Sanka's native envelope answer 501 with their",
                "# adaptation codes: an unmigrated route fails loudly instead of",
                "# silently 404ing. Replace each stub with a real handler; the",
                "# inventory lives in sanka-manifest.json under unsupported_routes.",
                "",
            ]
        )
        for entry in stubbed:
            method = str(entry["method"]).upper()
            path = str(entry["path"])
            body = json.dumps(
                {
                    "detail": (
                        "This route is outside Sanka's native generation envelope "
                        "and has not been migrated."
                    ),
                    "sanka": {
                        "route": f"{method} {path}",
                        "adaptation_codes": [
                            str(reason["code"]) for reason in entry.get("reasons", [])
                        ],
                        "see": "sanka-manifest.json#unsupported_routes",
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            func = _unique_ident("sanka_unsupported", used_funcs)
            lines.extend(
                [
                    f"@app.api_route({_py_str(path)}, methods=[{_py_str(method)}])",
                    f"async def {func}() -> Response:",
                    "    return Response(",
                    f"        content={_py_str(body)},",
                    "        status_code=501,",
                    '        media_type="application/json",',
                    "    )",
                    "",
                ]
            )
    lines.extend(
        [
            'if __name__ == "__main__":',
            "    import uvicorn",
            '    uvicorn.run(app, host="127.0.0.1", port=8000)',
            "",
        ]
    )
    lines.extend(
        [
            "",
            "",
            "# Django resolves the path before the method: an unknown path is a 404 (or an",
            "# APPEND_SLASH 301), a known path with a disallowed method is DRF's 405. Both",
            "# are served here by an APIRoute that lives in this file.",
            "_FALLBACK_METHODS = "
            '["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"]',
            "",
            "",
            '@app.api_route("/{path:path}", methods=_FALLBACK_METHODS, include_in_schema=False)',
            "async def django_fallback(request: Request, path: str) -> Response:",
            "    return await native.fallback_response(request, _DJANGO_DEFAULT_404)",
        ]
    )

    return "\n".join(lines)


_PARITY_CHECKLIST = """\
## Completing the migration

Reuse generated handlers and the helpers in `sanka_native.py` and `sanka_store.py`
where their behavior matches the source. Implement the remaining gaps, then run
`sanka test` and differential `sanka verify`; generated tests alone do not prove
source parity. The route references below scope each source-derived fact. Do not
apply a fact from one route to every handler."""


def _gap_report_payload(plan: FrameworkPlan, scan: FrameworkScan) -> dict[str, Any]:
    manual = sorted(
        (route for route in plan.routes if route.strategy == ROUTE_STRATEGY_MANUAL),
        key=lambda route: (route.path, route.method),
    )
    return {
        "schema": "sanka/native-gap-report/v1",
        "plan_hash": plan.plan_hash,
        "readiness": plan.readiness,
        "threshold_unit": "ratio",
        "native_routes": plan.native_routes,
        "native_eligible_routes": plan.native_eligible_routes,
        "needs_adaptation_routes": plan.needs_adaptation_routes,
        "unsupported_routes": [
            {
                "method": route.method,
                "path": route.path,
                "operation": route.operation,
                "source_view": route.source_view,
                "reasons": [
                    {"code": reason.code, "feature": reason.feature, "message": reason.message}
                    for reason in route.adaptation_reasons
                ],
                "parity_notes": [_parity_note_payload(note) for note in route.parity_notes],
            }
            for route in manual
        ],
        "skipped_routes": [
            {"pattern": item.pattern, "view": item.view, "reason": item.reason}
            for item in scan.skipped_routes
        ],
        "critic_checks": {
            "route_coverage": "required",
            "redirect_and_header_parity": "required",
            "native_serving_evidence": "required",
            "database_parity": "required",
        },
    }


def _render_gap_report(plan: FrameworkPlan, scan: FrameworkScan) -> str:
    manual = sorted(
        (route for route in plan.routes if route.strategy == ROUTE_STRATEGY_MANUAL),
        key=lambda route: (route.path, route.method),
    )
    lines = [
        "# Sanka native migration gap report",
        "",
        f"Plan `{plan.plan_hash}` — native readiness {plan.readiness:.0%} "
        f"({plan.native_routes}/{plan.native_eligible_routes} non-alias routes generatable).",
        "",
        "The source application remains the specification. Every route below",
        "still needs a hand-written handler whose behavior is verified against",
        "the source application, not assumed from generated code.",
        "",
    ]
    if manual:
        lines.append(f"## Routes needing manual adaptation ({len(manual)})")
        lines.append("")
        # Identical guidance is repeated across methods and format aliases. Keep its
        # full identity (including source location), with explicit per-route references.
        guidance: dict[tuple[str, str, str], str] = {}
        for route in manual:
            mounted = (
                "stubbed to answer 501 in the generated app"
                if _stub_safe_path(route.path)
                else "NOT mounted — the path is not representable as a FastAPI route"
            )
            references = []
            for reason in route.adaptation_reasons:
                key = (f"adaptation/{reason.feature} `{reason.code}`", reason.message, "")
                reference = guidance.setdefault(key, f"G{len(guidance) + 1}")
                references.append(reference)
            for note in route.parity_notes:
                key = (f"parity/{note.family} `{note.code}`", note.message, note.source or "")
                reference = guidance.setdefault(key, f"G{len(guidance) + 1}")
                references.append(reference)
            lines.append(f"- `{route.method} {route.path}` — {mounted}; " + ", ".join(references))
        lines.extend(["", "## Source-derived guidance", ""])
        for (label, message, source), reference in guidance.items():
            where = f" ({source})" if source else ""
            lines.append(f"- **{reference}** {label}: {message}{where}")
        lines.append("")
    else:
        lines.extend(
            [
                "## Routes needing manual adaptation (0)",
                "",
                "Every non-alias scanned route was generated natively.",
                "",
            ]
        )
    if scan.skipped_routes:
        lines.extend(
            [
                f"## URL patterns the scanner did not scan ({len(scan.skipped_routes)})",
                "",
                "Non-DRF Django views: they serve real traffic but are invisible to",
                "the DRF scan, so no readiness number accounts for them. Port them by",
                "hand.",
                "",
            ]
        )
        for item in scan.skipped_routes:
            lines.append(f"- `{item.pattern}` → `{item.view}` ({item.reason})")
        lines.append("")
    lines.append(_PARITY_CHECKLIST)
    lines.extend(
        [
            "",
            "## Machine-readable detail",
            "",
            "`plan-fastapi.json` beside this file carries per-route strategies and",
            "adaptation codes; a generated app's `sanka-manifest.json` repeats the",
            "inventory under `unsupported_routes` and `skipped_routes`.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_native_readme(
    plan: FrameworkPlan, sql_engine: str = "tortoise", *, entrypoint: str = "app.py"
) -> str:
    engine = sql_engine or "tortoise"
    runtime_dir = "app/generated/" if "/" in entrypoint else ""
    if not plan.database_required:
        persistence = (
            "No generated route requires database setup, so no database runtime is included."
        )
    elif engine == "django":
        persistence = (
            "Persistence uses the retained Django ORM through the async facade in "
            f"`{runtime_dir}sanka_store.py`. Generated `{runtime_dir}sanka_settings.py` "
            "removes DRF apps; Django is "
            "loaded for ORM access only, never as the request server."
        )
    else:
        persistence = (
            f"Persistence is async SQL (`{engine}`) in `{runtime_dir}sanka_store.py`, "
            "mapped onto the "
            "existing Django tables. Django is not imported at serve time."
        )
    gaps = ""
    if plan.needs_adaptation_routes:
        gaps = (
            f"\n**{plan.needs_adaptation_routes} route(s) are outside the native envelope "
            f"and were NOT migrated** (native readiness {plan.readiness:.0%}). Mountable "
            "ones are stubbed to answer 501 with their adaptation codes; the full "
            "inventory is `unsupported_routes` in `sanka-manifest.json`. For those "
            "routes the source application remains the specification.\n"
        )
    module = Path(entrypoint).with_suffix("").as_posix().replace("/", ".")
    test_location = "tests/test_generated.py" if "/" in entrypoint else "test_generated.py"
    setup = (
        f"uv sync\nuv run uvicorn {module}:app --reload"
        if plan.package_manager == "uv"
        else (
            "python -m venv .venv\n"
            ".venv/bin/python -m pip install -r requirements.txt "
            "-r requirements-test.txt\n"
            f".venv/bin/python -m uvicorn {module}:app --reload"
        )
    )
    database_setup = (
        "\nSet `SANKA_DATABASE_URL` for PostgreSQL (the scan never stores a password).\n"
        "SQLite uses the captured database path, overridable with `SANKA_DATABASE_URL`\n"
        "or `SANKA_TEST_DB`.\n"
        if plan.database_required
        else ""
    )
    docs = (
        "Swagger UI is enabled at `/docs`. Use a hostname permitted by the source "
        "Django `ALLOWED_HOSTS` (for example, `localhost` may work when `127.0.0.1` does not)."
        if plan.swagger_ui
        else "Swagger UI at `/docs` is disabled by the reviewed plan."
    )
    return f"""# Generated native FastAPI application

Sanka generated this application from plan `{plan.plan_hash}`.

Routes are declared with FastAPI decorators in `{entrypoint}` (`@app.get`,
`@app.post`, ...). Shared DRF-parity validation lives in `{runtime_dir}sanka_native.py`.
{persistence}
{gaps}
{database_setup}

`sanka test` writes `{test_location}` and runs it. SQLite write tests
use an isolated copy of the database.

Format-suffix alias routes from the source router are dropped as a disclosed
contract change; clients negotiate content types with headers instead.

{docs} OpenAPI remains at `/openapi.json`; ReDoc remains at `/redoc`.

```bash
{setup}
```
"""


def _render_compatibility_runtime() -> str:
    return """# Generated by Sanka under the license selected for this generated application.
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import django
from django.core.asgi import get_asgi_application
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

HERE = Path(__file__).resolve().parent
MANIFEST = json.loads((HERE / "sanka-manifest.json").read_text(encoding="utf-8"))
PROJECT_ROOT = HERE.parents[1] if MANIFEST.get("generation_mode") == "full" else HERE
SOURCE_ROOT = (PROJECT_ROOT / MANIFEST["source_root"]).resolve()
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))
os.environ["DJANGO_SETTINGS_MODULE"] = MANIFEST["settings_module"]
django.setup()
DJANGO_APP = get_asgi_application()

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


async def _dispatch(request: Request, method: str) -> Response:
    scope = dict(request.scope)
    scope["method"] = method
    scope["root_path"] = ""
    start = {}
    started = asyncio.Event()
    body_chunks = asyncio.Queue(maxsize=1)

    async def send(message):
        if message["type"] == "http.response.start":
            start.update(message)
            started.set()
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body:
                await body_chunks.put(body)

    async def run_app():
        try:
            await DJANGO_APP(scope, request.receive, send)
        finally:
            await body_chunks.put(None)

    app_task = asyncio.create_task(run_app())
    start_task = asyncio.create_task(started.wait())
    done, _pending = await asyncio.wait(
        {app_task, start_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if app_task in done and not started.is_set():
        await app_task
        start_task.cancel()
        return Response(content=b"", status_code=500)
    start_task.cancel()

    async def stream_body():
        try:
            while True:
                chunk = await body_chunks.get()
                if chunk is None:
                    break
                yield chunk
            await app_task
        finally:
            if not app_task.done():
                app_task.cancel()

    response = StreamingResponse(stream_body(), status_code=start["status"])
    response.raw_headers = [
        (key, value)
        for key, value in start.get("headers", [])
        if key.decode("latin-1").lower() not in HOP_BY_HOP
        and key.decode("latin-1").lower() != "content-length"
    ]
    return response


def create_app() -> FastAPI:
    app = FastAPI(
        title="Sanka DRF to FastAPI compatibility application",
        docs_url="/docs" if MANIFEST.get("swagger_ui", True) else None,
    )
    for index, route in enumerate(MANIFEST["routes"]):
        method = route["method"]

        def make_handler(route_method: str):
            async def handler(request: Request) -> Response:
                return await _dispatch(request, route_method)

            return handler

        handler = make_handler(method)
        handler.__name__ = "sanka_" + route["operation"] + "_" + str(index)
        app.add_api_route(
            route["path"],
            handler,
            methods=[method],
            operation_id=handler.__name__,
            tags=["Sanka compatibility bridge"],
        )
    return app
"""


def _render_generated_readme(plan: FrameworkPlan, *, entrypoint: str = "app.py") -> str:
    module = Path(entrypoint).with_suffix("").as_posix().replace("/", ".")
    setup = (
        f"uv sync\nuv run uvicorn {module}:app --reload"
        if plan.package_manager == "uv"
        else (
            "python -m venv .venv\n"
            ".venv/bin/python -m pip install -r requirements.txt "
            "-r requirements-test.txt\n"
            f".venv/bin/python -m uvicorn {module}:app --reload"
        )
    )
    docs = "enabled at `/docs`" if plan.swagger_ui else "disabled at `/docs`"
    return f"""# Generated FastAPI compatibility application

Sanka generated this application from plan `{plan.plan_hash}`.

It is a **compatibility bridge**, not a claim that Django REST Framework has
already been removed. FastAPI owns the generated route graph and forwards each
request into the existing Django application in-process so observable behavior
stays stable. Replace bridge routes with native FastAPI handlers incrementally,
keeping `sanka verify` green after each replacement.

Swagger UI is {docs}. OpenAPI remains at `/openapi.json`; ReDoc remains at `/redoc`.

Run locally from the generated project root:

```bash
{setup}
```

The generated application retains Django models, migrations, ORM,
authentication, permissions, and synchronous transaction handlers.
"""

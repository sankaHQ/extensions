# SPDX-License-Identifier: Apache-2.0
"""Stable artifacts for source-framework scans and target-framework plans."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any

from sanka_code_migration.drf.model import (  # isort: skip
    ApiRootIR as ApiRootIR,
    DatabaseIR as DatabaseIR,
    FrameworkRisk as FrameworkRisk,
    FrameworkScan as FrameworkScan,
    ParityNote as ParityNote,
    RouteAdaptationReason as RouteAdaptationReason,
    RouteIR as RouteIR,
    SerializerFieldIR as SerializerFieldIR,
    SerializerIR as SerializerIR,
    SkippedRoute as SkippedRoute,
    ViewAuthIR as ViewAuthIR,
    ViewIR as ViewIR,
    _strip_create_contract as _strip_create_contract,
    _strip_field_keys as _strip_field_keys,
)

from sanka_extension_drf_to_fastapi.hashing import content_hash


@dataclass(frozen=True, slots=True)
class PlannedRoute:
    method: str
    path: str
    operation: str
    source_view: str
    strategy: str
    automatic: bool
    adaptation_reasons: tuple[RouteAdaptationReason, ...] = ()
    parity_notes: tuple[ParityNote, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PlannedRoute:
        data = dict(payload)
        data["adaptation_reasons"] = tuple(
            RouteAdaptationReason.from_dict(item) for item in payload.get("adaptation_reasons", ())
        )
        data["parity_notes"] = tuple(
            ParityNote.from_dict(item) for item in payload.get("parity_notes", ())
        )
        return cls(**data)


@dataclass(frozen=True, slots=True)
class FileOperation:
    path: str
    action: str
    expected_hash: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> FileOperation:
        return cls(
            path=str(payload["path"]),
            action=str(payload["action"]),
            expected_hash=str(payload.get("expected_hash") or ""),
        )


@dataclass(frozen=True, slots=True)
class FrameworkPlan:
    schema_version: int
    source_framework: str
    target_framework: str
    mode: str
    source_scan_hash: str
    settings_module: str
    routes: tuple[PlannedRoute, ...]
    risks: tuple[FrameworkRisk, ...]
    retained: tuple[str, ...]
    default_output: str
    sql_engine: str = "tortoise"
    generation_mode: str = "minimal"
    target_generation_mode: str = ""
    package_manager: str = "uv"
    swagger_ui: bool = True
    database_required: bool = True
    target_fingerprint: str = ""
    file_operations: tuple[FileOperation, ...] = ()
    capabilities: tuple[str, ...] = ()
    omissions: tuple[str, ...] = ()
    plan_hash: str = field(default="")

    @property
    def automatic_routes(self) -> int:
        if self.mode == "native":
            return self.native_routes
        return sum(route.automatic for route in self.routes)

    @property
    def native_routes(self) -> int:
        return sum(
            route.strategy in ("native-fastapi-crud", "native-fastapi-api-root")
            for route in self.routes
        )

    @property
    def dropped_alias_routes(self) -> int:
        return sum(route.strategy == "dropped-format-suffix-alias" for route in self.routes)

    @property
    def native_eligible_routes(self) -> int:
        return len(self.routes) - self.dropped_alias_routes

    @property
    def needs_adaptation_routes(self) -> int:
        if self.mode != "native":
            return len(self.routes) - self.automatic_routes
        return sum(route.strategy == "needs-manual-adaptation" for route in self.routes)

    @property
    def alias_drop_rate(self) -> float:
        if not self.routes:
            return 0.0
        return self.dropped_alias_routes / len(self.routes)

    @property
    def readiness(self) -> float:
        if not self.routes:
            return 0.0
        if self.mode == "native":
            if not self.native_eligible_routes:
                return 0.0
            return self.native_routes / self.native_eligible_routes
        return self.automatic_routes / len(self.routes)

    def hash_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("plan_hash", None)
        if self.swagger_ui:
            payload.pop("swagger_ui")  # Keep hashes of existing default-on plans valid.
        if self.schema_version < 2:
            for route in payload["routes"]:
                route.pop("adaptation_reasons", None)
        if self.schema_version < 3:
            for key in (
                "generation_mode",
                "target_generation_mode",
                "package_manager",
                "database_required",
                "target_fingerprint",
                "file_operations",
                "capabilities",
                "omissions",
            ):
                payload.pop(key, None)
        if self.schema_version < 4:
            for route in payload["routes"]:
                route.pop("parity_notes", None)
        return payload

    def with_hash(self) -> FrameworkPlan:
        return replace(self, plan_hash=content_hash(self.hash_payload()))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["automatic_routes"] = self.automatic_routes
        payload["native_routes"] = self.native_routes
        payload["dropped_alias_routes"] = self.dropped_alias_routes
        payload["native_eligible_routes"] = self.native_eligible_routes
        payload["needs_adaptation_routes"] = self.needs_adaptation_routes
        payload["alias_drop_rate"] = self.alias_drop_rate
        payload["readiness"] = self.readiness
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> FrameworkPlan:
        swagger_ui = payload.get("swagger_ui", True)
        if not isinstance(swagger_ui, bool):
            raise ValueError("swagger_ui must be a boolean")
        return cls(
            schema_version=int(payload["schema_version"]),
            source_framework=str(payload["source_framework"]),
            target_framework=str(payload["target_framework"]),
            mode=str(payload["mode"]),
            source_scan_hash=str(payload["source_scan_hash"]),
            settings_module=str(payload["settings_module"]),
            routes=tuple(PlannedRoute.from_dict(item) for item in payload.get("routes", [])),
            risks=tuple(FrameworkRisk(**item) for item in payload.get("risks", [])),
            retained=tuple(payload.get("retained", [])),
            default_output=str(payload["default_output"]),
            sql_engine=str(payload.get("sql_engine") or "tortoise"),
            generation_mode=str(payload.get("generation_mode") or "minimal"),
            target_generation_mode=str(payload.get("target_generation_mode") or ""),
            package_manager=str(payload.get("package_manager") or "uv"),
            swagger_ui=swagger_ui,
            database_required=bool(payload.get("database_required", True)),
            target_fingerprint=str(payload.get("target_fingerprint") or ""),
            file_operations=tuple(
                FileOperation.from_dict(item) for item in payload.get("file_operations", ())
            ),
            capabilities=tuple(str(item) for item in payload.get("capabilities", ())),
            omissions=tuple(str(item) for item in payload.get("omissions", ())),
            plan_hash=str(payload.get("plan_hash", "")),
        )

# SPDX-License-Identifier: Apache-2.0
"""Deterministic Flask architecture policy derived only from captured facts."""

from __future__ import annotations

from .ir import BackendIR, DecisionReason, TargetProfile

_CONFIG_FIELDS = {
    "target",
    "orm",
    "generation",
    "strategy",
    "package_manager",
    "database",
    "layers",
    "completion_policy",
}


def _string(config: dict[str, object], name: str, default: str) -> str:
    value = config.get(name, default)
    if type(value) is not str:
        raise TypeError(f"{name} must be a string")
    return value


def _nested(config: dict[str, object], name: str, fields: set[str]) -> dict[str, object]:
    value = config.get(name, {})
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise TypeError(f"{name} must be an object")
    unknown = set(value) - fields
    if unknown:
        raise ValueError(f"unknown {name} fields: {', '.join(sorted(unknown))}")
    return value


def _service_reasons(ir: BackendIR) -> tuple[tuple[str, ...], list[DecisionReason]]:
    services: list[str] = []
    reasons: list[DecisionReason] = []
    for operation in ir.operations:
        facts = []
        if operation.coordinates_multiple_writes:
            facts.append("coordinates multiple writes under one transaction")
        if operation.supported_external_boundary:
            facts.append("integrates a supported external boundary")
        if operation.shared_domain_logic:
            facts.append("shares domain logic between entrypoints")
        selected = bool(facts)
        if selected:
            services.append(operation.name)
        reasons.append(
            DecisionReason(
                kind="service",
                name=operation.name,
                selected=selected,
                reason="; ".join(facts)
                if facts
                else "direct operation has no recognized service boundary",
                evidence=operation.evidence,
            )
        )
    return tuple(services), reasons


def _repository_reasons(
    ir: BackendIR, services: tuple[str, ...]
) -> tuple[tuple[str, ...], list[DecisionReason]]:
    operations = {operation.name: operation for operation in ir.operations}
    candidates = sorted(set(operations) | set(ir.reusable_queries))
    repositories: list[str] = []
    reasons: list[DecisionReason] = []
    for name in candidates:
        operation = operations.get(name)
        facts = []
        if name in ir.reusable_queries:
            facts.append("source has a supported reusable query component")
        if name in services and operation and operation.needs_persistence_boundary:
            facts.append("service requires a persistence boundary")
        selected = bool(facts)
        if selected:
            repositories.append(name)
        reasons.append(
            DecisionReason(
                kind="repository",
                name=name,
                selected=selected,
                reason="; ".join(facts)
                if facts
                else "no reusable query or service persistence boundary",
                evidence=operation.evidence if operation else (),
            )
        )
    return tuple(repositories), reasons


def resolve_profile(ir: BackendIR, config: dict[str, object]) -> TargetProfile:
    """Resolve one strict Flask profile without consulting the local environment."""

    if type(ir) is not BackendIR:
        raise TypeError("ir must be BackendIR")
    if type(config) is not dict or any(type(key) is not str for key in config):
        raise TypeError("config must be an object with string keys")
    unknown = set(config) - _CONFIG_FIELDS
    if unknown:
        raise ValueError(f"unknown config fields: {', '.join(sorted(unknown))}")

    target = _string(config, "target", "flask")
    if target != "flask":
        raise ValueError(f"unsupported target: {target}")
    orm = _string(config, "orm", "django")
    if orm not in {"sqlalchemy", "django", "none"}:
        raise ValueError(f"unsupported orm: {orm}")
    generation = _string(config, "generation", "minimal")
    if generation not in {"auto", "minimal", "full"}:
        raise ValueError(f"unsupported generation: {generation}")
    if _string(config, "strategy", "native") != "native":
        raise ValueError("unsupported strategy")
    if _string(config, "package_manager", "uv") != "uv":
        raise ValueError("unsupported package_manager")
    if _string(config, "completion_policy", "strict") != "strict":
        raise ValueError("unsupported completion_policy")

    layers = _nested(config, "layers", {"services", "repositories"})
    for name in ("services", "repositories"):
        if name in layers and layers[name] != "auto":
            raise ValueError(f"layers.{name} supports only auto")
    database = _nested(config, "database", {"dialect", "schema_mode"})
    requested_dialect = database.get("dialect", "preserve")
    if type(requested_dialect) is not str:
        raise TypeError("database.dialect must be a string")
    if requested_dialect not in {"preserve", "sqlite", "postgresql"}:
        raise ValueError("unsupported database dialect")
    if requested_dialect != "preserve" and requested_dialect != ir.database_dialect:
        raise ValueError("database dialect changes are unsupported")

    if orm == "none" and ir.persistence:
        raise ValueError("orm=none requires persistence=false")
    if orm != "none" and not ir.persistence:
        raise ValueError(f"orm={orm} requires persistence=true")
    dialect = ir.database_dialect
    default_schema_mode = {
        "sqlalchemy": "adopt-existing",
        "django": "source-owned",
        "none": "none",
    }[orm]
    requested_schema_mode = database.get("schema_mode", default_schema_mode)
    if type(requested_schema_mode) is not str:
        raise TypeError("database.schema_mode must be a string")
    allowed_schema_modes = (
        {"empty", "adopt-existing"} if orm == "sqlalchemy" else {default_schema_mode}
    )
    if requested_schema_mode not in allowed_schema_modes:
        raise ValueError(
            f"orm={orm} requires schema_mode in {', '.join(sorted(allowed_schema_modes))}"
        )
    schema_mode = requested_schema_mode

    services, service_reasons = _service_reasons(ir)
    repositories, repository_reasons = _repository_reasons(ir, services)
    if generation == "minimal":
        layout = "minimal"
        layout_reason = "minimal layout was explicitly requested"
    elif generation == "full":
        layout = "modular"
        layout_reason = "full organization was explicitly requested"
    elif len(ir.route_groups) > 1:
        layout = "modular"
        layout_reason = "multiple independently owned route groups require modular layout"
    elif services:
        layout = "modular"
        layout_reason = "a required service boundary requires modular layout"
    else:
        layout = "minimal"
        layout_reason = "one route group and no service boundary fit the compact layout"

    return TargetProfile(
        profile_version="flask-profile/v1",
        target=target,
        orm=orm,
        dialect=dialect,
        schema_mode=schema_mode,
        layout=layout,
        services=services,
        repositories=repositories,
        dependency_locks=(),
        toolchain_locks=(),
        locks_complete=False,
        reasons=(
            DecisionReason(
                kind="layout",
                name="application",
                selected=layout == "modular",
                reason=layout_reason,
            ),
            *service_reasons,
            *repository_reasons,
        ),
    )

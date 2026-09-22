# SPDX-License-Identifier: Apache-2.0
"""Immutable JSON contracts used by backend migration planning."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import ClassVar

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_EXACT_VERSION = re.compile(r"(?:==)?[A-Za-z0-9][A-Za-z0-9._+-]*\Z")
_ABSOLUTE_PATH = re.compile(r"(?:/|[A-Za-z]:[\\/])")
_SECRET_PARTS = {"PASSWORD", "SECRET", "TOKEN", "CREDENTIAL"}


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise TypeError(f"{label} must be a non-empty string")
    return value


def _portable_json(value: object, label: str = "source setting") -> None:
    if value is None or type(value) in {bool, int, float}:
        _canonical_json(value)
        return
    if type(value) is str:
        return
    if type(value) is list:
        for item in value:
            _portable_json(item, label)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for key, item in value.items():
            if any(part in _SECRET_PARTS for part in key.upper().split("_")):
                raise ValueError(f"{label} must omit secret values")
            _portable_json(item, label)
        return
    raise TypeError(f"{label} must contain only JSON values")


def _portable_text(value: str, label: str) -> str:
    value = _text(value, label)
    if _ABSOLUTE_PATH.match(value):
        raise ValueError(f"{label} must be portable and source-relative")
    return value


def _unique(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {label}")


@dataclass(frozen=True, slots=True)
class SourceSetting:
    """One effective source setting stored as canonical JSON."""

    name: str
    value_json: str

    def __post_init__(self) -> None:
        _text(self.name, "source setting name")
        if any(part in _SECRET_PARTS for part in self.name.upper().split("_")):
            raise ValueError("source setting name must omit secret values")
        _text(self.value_json, "source setting value_json")
        try:
            value = json.loads(self.value_json)
        except json.JSONDecodeError as error:
            raise ValueError("source setting value_json must be JSON") from error
        _portable_json(value)
        if self.value_json != _canonical_json(value):
            raise ValueError("source setting value_json must be canonical JSON")

    @classmethod
    def from_value(cls, name: str, value: object) -> SourceSetting:
        _portable_json(value)
        return cls(name=name, value_json=_canonical_json(value))

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "value": json.loads(self.value_json)}


@dataclass(frozen=True, slots=True)
class VersionPin:
    name: str
    version: str

    def __post_init__(self) -> None:
        _text(self.name, "pin name")
        _text(self.version, "pin version")
        if self.version == "latest" or not _EXACT_VERSION.fullmatch(self.version):
            raise ValueError("pin version must be one exact version")

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "version": self.version}


@dataclass(frozen=True, slots=True)
class OperationFact:
    """Only facts that affect service and repository planning."""

    name: str
    coordinates_multiple_writes: bool = False
    supported_external_boundary: bool = False
    shared_domain_logic: bool = False
    needs_persistence_boundary: bool = False
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.name, "operation name")
        for field in (
            "coordinates_multiple_writes",
            "supported_external_boundary",
            "shared_domain_logic",
            "needs_persistence_boundary",
        ):
            if type(getattr(self, field)) is not bool:
                raise TypeError(f"{field} must be a boolean")
        if type(self.evidence) is not tuple:
            raise TypeError("operation evidence must be a tuple")
        object.__setattr__(
            self,
            "evidence",
            tuple(sorted(_portable_text(item, "operation evidence") for item in self.evidence)),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "coordinates_multiple_writes": self.coordinates_multiple_writes,
            "supported_external_boundary": self.supported_external_boundary,
            "shared_domain_logic": self.shared_domain_logic,
            "needs_persistence_boundary": self.needs_persistence_boundary,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class DecisionReason:
    kind: str
    name: str
    selected: bool
    reason: str
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in {"layout", "service", "repository"}:
            raise ValueError("decision reason kind is unsupported")
        _text(self.name, "decision name")
        if type(self.selected) is not bool:
            raise TypeError("decision selected must be a boolean")
        _text(self.reason, "decision reason")
        if type(self.evidence) is not tuple:
            raise TypeError("decision evidence must be a tuple")
        object.__setattr__(self, "evidence", tuple(sorted(self.evidence)))

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "name": self.name,
            "selected": self.selected,
            "reason": self.reason,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class BackendIR:
    SCHEMA: ClassVar[str] = "sanka.backend-ir/v1"

    source_content_digest: str
    source_dependency_lock_digest: str
    source_settings: tuple[SourceSetting, ...]
    persistence: bool
    database_dialect: str
    route_groups: tuple[str, ...]
    operations: tuple[OperationFact, ...]
    reusable_queries: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for value, label in (
            (self.source_content_digest, "source_content_digest"),
            (self.source_dependency_lock_digest, "source_dependency_lock_digest"),
        ):
            if type(value) is not str or not _DIGEST.fullmatch(value):
                raise ValueError(f"{label} must be a canonical sha256 digest")
        if type(self.persistence) is not bool:
            raise TypeError("persistence must be a boolean")
        if self.database_dialect not in {"sqlite", "postgresql", "none"}:
            raise ValueError("database_dialect is unsupported")
        if self.persistence != (self.database_dialect != "none"):
            raise ValueError("database_dialect must be none exactly when persistence is false")
        if type(self.source_settings) is not tuple or any(
            type(item) is not SourceSetting for item in self.source_settings
        ):
            raise TypeError("source_settings must be a tuple of SourceSetting")
        if type(self.operations) is not tuple or any(
            type(item) is not OperationFact for item in self.operations
        ):
            raise TypeError("operations must be a tuple of OperationFact")
        if type(self.route_groups) is not tuple or any(
            type(item) is not str or not item for item in self.route_groups
        ):
            raise TypeError("route_groups must be a tuple of non-empty strings")
        if len(self.route_groups) != len(set(self.route_groups)):
            raise ValueError("route_groups must not contain duplicates")
        if type(self.reusable_queries) is not tuple or any(
            type(item) is not str or not item for item in self.reusable_queries
        ):
            raise TypeError("reusable_queries must be a tuple of non-empty strings")
        if len(self.reusable_queries) != len(set(self.reusable_queries)):
            raise ValueError("reusable_queries must not contain duplicates")
        setting_names = [item.name for item in self.source_settings]
        if len(setting_names) != len(set(setting_names)):
            raise ValueError("source_settings must not contain duplicate names")
        operation_names = [item.name for item in self.operations]
        if len(operation_names) != len(set(operation_names)):
            raise ValueError("operations must not contain duplicate names")
        object.__setattr__(
            self, "source_settings", tuple(sorted(self.source_settings, key=lambda item: item.name))
        )
        object.__setattr__(
            self, "operations", tuple(sorted(self.operations, key=lambda item: item.name))
        )
        object.__setattr__(self, "reusable_queries", tuple(sorted(self.reusable_queries)))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "source_content_digest": self.source_content_digest,
            "source_dependency_lock_digest": self.source_dependency_lock_digest,
            "source_settings": [item.to_dict() for item in self.source_settings],
            "persistence": self.persistence,
            "database_dialect": self.database_dialect,
            "route_groups": list(self.route_groups),
            "operations": [item.to_dict() for item in self.operations],
            "reusable_queries": list(self.reusable_queries),
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    def digest(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class TargetProfile:
    SCHEMA: ClassVar[str] = "sanka.target-profile/v1"

    profile_version: str
    target: str
    orm: str
    dialect: str
    schema_mode: str
    layout: str
    services: tuple[str, ...]
    repositories: tuple[str, ...]
    dependency_locks: tuple[VersionPin, ...]
    toolchain_locks: tuple[VersionPin, ...]
    locks_complete: bool
    reasons: tuple[DecisionReason, ...]

    def __post_init__(self) -> None:
        _text(self.profile_version, "profile_version")
        if self.target != "flask":
            raise ValueError("unsupported target")
        if self.orm not in {"sqlalchemy", "django", "none"}:
            raise ValueError("unsupported orm")
        if self.dialect not in {"sqlite", "postgresql", "none"}:
            raise ValueError("unsupported dialect")
        if self.schema_mode not in {"empty", "adopt-existing", "source-owned", "none"}:
            raise ValueError("unsupported schema_mode")
        if self.layout not in {"minimal", "modular"}:
            raise ValueError("unsupported layout")
        if type(self.dependency_locks) is not tuple or any(
            type(item) is not VersionPin for item in self.dependency_locks
        ):
            raise TypeError("dependency_locks must be a tuple of VersionPin")
        if type(self.toolchain_locks) is not tuple or any(
            type(item) is not VersionPin for item in self.toolchain_locks
        ):
            raise TypeError("toolchain_locks must be a tuple of VersionPin")
        if type(self.reasons) is not tuple or any(
            type(item) is not DecisionReason for item in self.reasons
        ):
            raise TypeError("reasons must be a tuple of DecisionReason")
        if type(self.locks_complete) is not bool:
            raise TypeError("locks_complete must be a boolean")
        if self.locks_complete and (not self.dependency_locks or not self.toolchain_locks):
            raise ValueError("complete locks require dependency and toolchain pins")
        for value, label in ((self.services, "services"), (self.repositories, "repositories")):
            if type(value) is not tuple or any(type(item) is not str or not item for item in value):
                raise TypeError(f"{label} must be a tuple of non-empty strings")
            _unique(value, label)
        _unique(tuple(item.name for item in self.dependency_locks), "dependency lock names")
        _unique(tuple(item.name for item in self.toolchain_locks), "toolchain lock names")
        object.__setattr__(self, "services", tuple(sorted(self.services)))
        object.__setattr__(self, "repositories", tuple(sorted(self.repositories)))
        object.__setattr__(
            self,
            "dependency_locks",
            tuple(sorted(self.dependency_locks, key=lambda item: item.name)),
        )
        object.__setattr__(
            self, "toolchain_locks", tuple(sorted(self.toolchain_locks, key=lambda item: item.name))
        )
        object.__setattr__(
            self, "reasons", tuple(sorted(self.reasons, key=lambda item: (item.kind, item.name)))
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "profile_version": self.profile_version,
            "target": self.target,
            "orm": self.orm,
            "dialect": self.dialect,
            "schema_mode": self.schema_mode,
            "layout": self.layout,
            "services": list(self.services),
            "repositories": list(self.repositories),
            "dependency_locks": [item.to_dict() for item in self.dependency_locks],
            "toolchain_locks": [item.to_dict() for item in self.toolchain_locks],
            "locks_complete": self.locks_complete,
            "reasons": [item.to_dict() for item in self.reasons],
        }

    def with_locks(
        self,
        *,
        dependency_locks: tuple[VersionPin, ...],
        toolchain_locks: tuple[VersionPin, ...],
    ) -> TargetProfile:
        """Return a qualified copy after integration supplies reviewed exact pins."""

        return replace(
            self,
            dependency_locks=dependency_locks,
            toolchain_locks=toolchain_locks,
            locks_complete=True,
        )

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    def digest(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class EffectiveInputs:
    """Every portable input whose change must invalidate deterministic output."""

    SCHEMA: ClassVar[str] = "sanka.migration-inputs/v1"

    backend_ir: BackendIR
    profile: TargetProfile
    generator_version: str
    formatter_locks: tuple[VersionPin, ...] = ()

    def __post_init__(self) -> None:
        if type(self.backend_ir) is not BackendIR or type(self.profile) is not TargetProfile:
            raise TypeError("effective inputs require BackendIR and TargetProfile")
        _text(self.generator_version, "generator_version")
        if type(self.formatter_locks) is not tuple or any(
            type(item) is not VersionPin for item in self.formatter_locks
        ):
            raise TypeError("formatter_locks must be a tuple of VersionPin")
        _unique(tuple(item.name for item in self.formatter_locks), "formatter lock names")
        object.__setattr__(
            self, "formatter_locks", tuple(sorted(self.formatter_locks, key=lambda item: item.name))
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "backend_ir": self.backend_ir.to_dict(),
            "profile": self.profile.to_dict(),
            "generator_version": self.generator_version,
            "formatter_locks": [item.to_dict() for item in self.formatter_locks],
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    def digest(self) -> str:
        return _digest(self.to_dict())

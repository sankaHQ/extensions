# SPDX-License-Identifier: Apache-2.0

import json
from dataclasses import FrozenInstanceError

import pytest
from sanka_code_migration import (
    BackendIR,
    EffectiveInputs,
    OperationFact,
    SourceSetting,
    VersionPin,
    resolve_profile,
)

SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64


def backend_ir(
    *,
    persistence: bool = True,
    database_dialect: str = "sqlite",
    route_groups: tuple[str, ...] = ("items",),
    operations: tuple[OperationFact, ...] = (OperationFact(name="items"),),
    reusable_queries: tuple[str, ...] = (),
    source_settings: tuple[SourceSetting, ...] = (),
) -> BackendIR:
    return BackendIR(
        source_content_digest=SHA_A,
        source_dependency_lock_digest=SHA_B,
        source_settings=source_settings,
        persistence=persistence,
        database_dialect=database_dialect,
        route_groups=route_groups,
        operations=operations,
        reusable_queries=reusable_queries,
    )


def test_simple_crud_uses_compact_sqlalchemy_profile_without_dead_layers() -> None:
    profile = resolve_profile(
        backend_ir(),
        {
            "orm": "sqlalchemy",
            "generation": "auto",
            "database": {"dialect": "preserve", "schema_mode": "adopt-existing"},
        },
    )

    assert profile.target == "flask"
    assert profile.orm == "sqlalchemy"
    assert profile.dialect == "sqlite"
    assert profile.schema_mode == "adopt-existing"
    assert profile.layout == "minimal"
    assert profile.services == ()
    assert profile.repositories == ()
    assert profile.dependency_locks == ()
    assert profile.toolchain_locks == ()
    assert any(
        reason.kind == "service" and reason.name == "items" and not reason.selected
        for reason in profile.reasons
    )


def test_architecture_facts_drive_services_repositories_and_auto_layout() -> None:
    ir = backend_ir(
        route_groups=("orders",),
        operations=(
            OperationFact(
                name="orders",
                coordinates_multiple_writes=True,
                needs_persistence_boundary=True,
                evidence=("shop/services.py:12",),
            ),
        ),
    )

    profile = resolve_profile(ir, {"orm": "sqlalchemy", "generation": "auto"})

    assert profile.layout == "modular"
    assert profile.services == ("orders",)
    assert profile.repositories == ("orders",)
    assert any(
        reason.kind == "service"
        and reason.name == "orders"
        and reason.selected
        and reason.evidence == ("shop/services.py:12",)
        for reason in profile.reasons
    )


def test_reusable_query_is_a_repository_fact_without_inventing_a_service() -> None:
    profile = resolve_profile(
        backend_ir(reusable_queries=("inventory",)),
        {"orm": "django", "generation": "minimal"},
    )

    assert profile.schema_mode == "source-owned"
    assert profile.layout == "minimal"
    assert profile.services == ()
    assert profile.repositories == ("inventory",)


@pytest.mark.parametrize(
    ("config", "match"),
    [
        ({"orm": "sqlalchemy", "surprise": True}, "unknown config fields"),
        ({"orm": 1}, "orm must be a string"),
        ({"orm": "tortoise"}, "unsupported orm"),
        ({"target": "fastapi", "orm": "sqlalchemy"}, "unsupported target"),
        (
            {"orm": "sqlalchemy", "database": {"dialect": "postgresql"}},
            "database dialect changes are unsupported",
        ),
    ],
)
def test_config_is_strict_and_dialect_changes_fail_before_generation(
    config: dict[str, object], match: str
) -> None:
    with pytest.raises((TypeError, ValueError), match=match):
        resolve_profile(backend_ir(), config)


def test_orm_none_requires_an_application_without_persistence() -> None:
    no_database = backend_ir(
        persistence=False,
        database_dialect="none",
        operations=(OperationFact(name="status"),),
    )

    profile = resolve_profile(no_database, {"orm": "none", "generation": "full"})

    assert profile.dialect == "none"
    assert profile.schema_mode == "none"
    assert profile.layout == "modular"
    with pytest.raises(ValueError, match="orm=none requires persistence=false"):
        resolve_profile(backend_ir(), {"orm": "none"})


def test_backend_ir_emission_is_versioned_canonical_and_immutable() -> None:
    settings_a = (
        SourceSetting.from_value("REST_FRAMEWORK", {"DEFAULT_RENDERER_CLASSES": ["json"]}),
        SourceSetting.from_value("APPEND_SLASH", True),
    )
    settings_b = tuple(reversed(settings_a))
    operations_a = (
        OperationFact(name="zeta", shared_domain_logic=True),
        OperationFact(name="alpha"),
    )
    operations_b = tuple(reversed(operations_a))
    first = backend_ir(operations=operations_a, source_settings=settings_a)
    second = backend_ir(operations=operations_b, source_settings=settings_b)

    assert first.to_json() == second.to_json()
    assert first.digest() == second.digest()
    assert json.loads(first.to_json())["schema"] == "sanka.backend-ir/v1"
    with pytest.raises(FrozenInstanceError):
        first.persistence = False  # type: ignore[misc]


def test_route_auth_and_middleware_order_remain_semantic() -> None:
    first = backend_ir(route_groups=("public", "admin"))
    second = backend_ir(route_groups=("admin", "public"))

    assert first.to_json() != second.to_json()


def test_effective_inputs_bind_portable_source_profile_and_real_pins() -> None:
    ir = backend_ir(
        source_settings=(SourceSetting.from_value("APPEND_SLASH", True),),
    )
    profile = resolve_profile(ir, {"orm": "sqlalchemy"})
    inputs = EffectiveInputs(
        backend_ir=ir,
        profile=profile,
        generator_version="0.1.0a1",
        formatter_locks=(VersionPin(name="ruff", version="0.16.5"),),
    )

    assert json.loads(inputs.to_json())["schema"] == "sanka.migration-inputs/v1"
    assert inputs.digest().startswith("sha256:")
    changed_source = EffectiveInputs(
        backend_ir=backend_ir(
            source_settings=(SourceSetting.from_value("APPEND_SLASH", False),),
        ),
        profile=profile,
        generator_version="0.1.0a1",
        formatter_locks=(VersionPin(name="ruff", version="0.16.5"),),
    )
    changed_generator = EffectiveInputs(
        backend_ir=ir,
        profile=profile,
        generator_version="0.1.0a2",
        formatter_locks=(VersionPin(name="ruff", version="0.16.5"),),
    )
    assert changed_source.digest() != inputs.digest()
    assert changed_generator.digest() != inputs.digest()
    assert profile.locks_complete is False
    locked = profile.with_locks(
        dependency_locks=(VersionPin(name="flask", version="3.1.2"),),
        toolchain_locks=(VersionPin(name="python", version="3.12.13"),),
    )
    assert locked.locks_complete is True
    assert profile.dependency_locks == ()
    with pytest.raises(ValueError, match="duplicate dependency lock names"):
        profile.with_locks(
            dependency_locks=(
                VersionPin(name="flask", version="3.1.2"),
                VersionPin(name="flask", version="3.1.1"),
            ),
            toolchain_locks=(VersionPin(name="python", version="3.12.13"),),
        )
    for unpinned in ("latest", "^0.16.5", ">=0.16.5"):
        with pytest.raises(ValueError, match="exact version"):
            VersionPin(name="ruff", version=unpinned)
    with pytest.raises(ValueError, match="secret"):
        SourceSetting.from_value("SECRET_KEY", "not-for-artifacts")


@pytest.mark.parametrize("schema_mode", ["empty", "adopt-existing"])
def test_native_sqlalchemy_supports_both_schema_ownership_modes(schema_mode: str) -> None:
    profile = resolve_profile(
        backend_ir(),
        {"orm": "sqlalchemy", "database": {"schema_mode": schema_mode}},
    )

    assert profile.schema_mode == schema_mode


def test_postgresql_is_preserved_and_multiple_route_groups_select_modular_layout() -> None:
    profile = resolve_profile(
        backend_ir(database_dialect="postgresql", route_groups=("orders", "billing")),
        {"orm": "sqlalchemy", "generation": "auto"},
    )

    assert profile.dialect == "postgresql"
    assert profile.layout == "modular"

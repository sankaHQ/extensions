# SPDX-License-Identifier: Apache-2.0
"""A MySQL plan cannot be read as a v1 SQLite/PostgreSQL plan."""

from dataclasses import replace

import pytest
from sanka_code_migration.ir import BackendIR, OperationFact, TargetProfile, VersionPin
from sanka_code_migration.ir_v2 import BackendIRV2, EffectiveInputsV2, TargetProfileV2


def source(cls=BackendIRV2, dialect="mysql"):
    return cls(
        source_content_digest="sha256:" + "a" * 64,
        source_dependency_lock_digest="sha256:" + "b" * 64,
        source_settings=(),
        persistence=True,
        database_dialect=dialect,
        route_groups=("/items",),
        operations=(OperationFact("GET /items", needs_persistence_boundary=True),),
    )


def profile(cls=TargetProfileV2, target="fastapi", orm="sqlalchemy", dialect="mysql"):
    return cls(
        profile_version="php-v1",
        target=target,
        orm=orm,
        dialect=dialect,
        schema_mode="adopt-existing",
        layout="modular",
        services=(),
        repositories=("items",),
        dependency_locks=(VersionPin("driver", "1.0.0"),),
        toolchain_locks=(VersionPin("php", "8.4.27"),),
        locks_complete=True,
        reasons=(),
    )


def test_mysql_requires_versioned_source_and_target_contracts():
    with pytest.raises(ValueError):
        source(BackendIR)
    with pytest.raises(ValueError):
        profile(TargetProfile, "flask", "sqlalchemy")
    inputs = EffectiveInputsV2(source(), profile(), "0.1.0a1")
    wire = inputs.to_dict()
    assert wire["schema"] == "sanka.migration-inputs/v2"
    assert wire["backend_ir"]["schema"] == "sanka.backend-ir/v2"
    assert wire["profile"]["schema"] == "sanka.target-profile/v2"
    assert wire["profile"]["dialect"] == "mysql"
    assert (
        replace(inputs, profile=profile(target="chi", orm="database-sql")).digest()
        != inputs.digest()
    )


def test_existing_profile_bytes_are_unchanged_by_v2():
    legacy = source(BackendIR, "sqlite")
    modern = source(BackendIRV2, "sqlite")
    assert legacy.to_dict() == modern.to_dict() | {"schema": "sanka.backend-ir/v1"}
    old_profile = profile(TargetProfile, "flask", "sqlalchemy", "sqlite")
    new_profile = profile(TargetProfileV2, "flask", "sqlalchemy", "sqlite")
    assert old_profile.to_dict() == new_profile.to_dict() | {"schema": "sanka.target-profile/v1"}


@pytest.mark.parametrize("target,orm", [("chi", "django"), ("fastapi", "database-sql")])
def test_incompatible_target_database_layers_are_rejected(target, orm):
    with pytest.raises(ValueError):
        profile(target=target, orm=orm)


def test_mysql_cannot_silently_become_a_different_database():
    with pytest.raises(ValueError):
        EffectiveInputsV2(source(), profile(dialect="sqlite"), "0.1.0a1")

# SPDX-License-Identifier: Apache-2.0
"""Opt-in native API contracts including MySQL; v1 plans remain immutable."""

from __future__ import annotations

from typing import ClassVar

from .ir import BackendIR, EffectiveInputs, TargetProfile


class BackendIRV2(BackendIR):
    SCHEMA: ClassVar[str] = "sanka.backend-ir/v2"
    DIALECTS: ClassVar[frozenset[str]] = BackendIR.DIALECTS | {"mysql"}


class TargetProfileV2(TargetProfile):
    SCHEMA: ClassVar[str] = "sanka.target-profile/v2"
    TARGETS: ClassVar[frozenset[str]] = TargetProfile.TARGETS | {"fastapi", "chi"}
    ORMS: ClassVar[frozenset[str]] = TargetProfile.ORMS | {"database-sql"}
    DIALECTS: ClassVar[frozenset[str]] = BackendIRV2.DIALECTS

    def __post_init__(self) -> None:
        super().__post_init__()
        layers = {
            "fastapi": {"sqlalchemy", "none"},
            "chi": {"database-sql", "none"},
            "flask": {"sqlalchemy", "django", "none"},
        }
        if self.orm not in layers[self.target]:
            raise ValueError("database layer does not implement the selected target")
        if (self.orm == "none") != (self.dialect == "none"):
            raise ValueError("database layer and dialect must agree on persistence")


class EffectiveInputsV2(EffectiveInputs):
    SCHEMA: ClassVar[str] = "sanka.migration-inputs/v2"
    IR_TYPE: ClassVar[type[BackendIR]] = BackendIRV2
    PROFILE_TYPE: ClassVar[type[TargetProfile]] = TargetProfileV2

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.backend_ir.database_dialect != self.profile.dialect:
            raise ValueError("v2 native API profiles preserve the source database engine")

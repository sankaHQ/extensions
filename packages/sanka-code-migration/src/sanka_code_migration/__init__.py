# SPDX-License-Identifier: Apache-2.0
"""Deterministic facts and policy shared by backend migration extensions."""

from .ir import (
    BackendIR,
    DecisionReason,
    EffectiveInputs,
    OperationFact,
    SourceSetting,
    TargetProfile,
    VersionPin,
)
from .policy import resolve_profile

__all__ = [
    "BackendIR",
    "DecisionReason",
    "EffectiveInputs",
    "OperationFact",
    "SourceSetting",
    "TargetProfile",
    "VersionPin",
    "resolve_profile",
]

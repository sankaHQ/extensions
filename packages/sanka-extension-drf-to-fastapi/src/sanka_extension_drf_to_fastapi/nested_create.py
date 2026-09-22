# SPDX-License-Identifier: Apache-2.0
"""Compatibility exports for shared DRF source contracts."""

from sanka_code_migration.drf.nested_create import (  # isort: skip
    lower_nested_create as lower_nested_create,
    supports_default_model_writes as supports_default_model_writes,
)

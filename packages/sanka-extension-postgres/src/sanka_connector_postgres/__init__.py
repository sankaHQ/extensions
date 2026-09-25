# SPDX-License-Identifier: Apache-2.0
"""Compatibility import for the published data extension module."""

import sys
from importlib import import_module

for part in ("_base", "_source", "_destination"):
    sys.modules[f"{__name__}.{part}"] = import_module(f"sanka_extension_postgres.{part}")
sys.modules[__name__] = import_module("sanka_extension_postgres")

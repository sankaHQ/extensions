# SPDX-License-Identifier: Apache-2.0
"""Compatibility import for the published data extension module."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("sanka_extension_sqlite")

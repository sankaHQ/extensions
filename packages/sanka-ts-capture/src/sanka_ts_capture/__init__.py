# SPDX-License-Identifier: Apache-2.0
"""Bounded TypeScript syntax capture shared by Sanka code extensions."""

from . import tree
from .driver import (
    MIN_NODE_MAJOR,
    TYPESCRIPT_SHA256,
    TYPESCRIPT_VERSION,
    Diagnostic,
    ParsedFile,
    TypeScriptDriverError,
    bundle_path,
    node_executable,
    node_version,
    parse_sources,
    transpile_sources,
)

__all__ = [
    "MIN_NODE_MAJOR",
    "TYPESCRIPT_SHA256",
    "TYPESCRIPT_VERSION",
    "Diagnostic",
    "ParsedFile",
    "TypeScriptDriverError",
    "bundle_path",
    "node_executable",
    "node_version",
    "parse_sources",
    "transpile_sources",
    "tree",
]

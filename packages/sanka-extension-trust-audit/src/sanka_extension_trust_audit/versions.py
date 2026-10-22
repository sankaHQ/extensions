# SPDX-License-Identifier: Apache-2.0
"""Version rules for runtimes and libraries that ship their own root store."""

from __future__ import annotations

import re

from . import advisory

_EXACT = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_GO = re.compile(r"^(?:go)?(\d+)\.(\d+)(?:\.(\d+))?$")


def parse(value: str) -> tuple[int, ...] | None:
    parts = value.strip().lstrip("v").split(".")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def below(value: str, safe: str) -> bool:
    current, minimum = parse(value), parse(safe)
    return current is not None and minimum is not None and current < minimum


def node_verdict(value: str) -> tuple[str, str | None] | None:
    """Classify a Node.js version: ("fixed"|"vulnerable"|"unsupported", target)."""
    match = _EXACT.match(value.strip())
    if not match:
        return None
    major = int(match.group(1))
    if major > advisory.NODE_LAST_AFFECTED_MAJOR:
        return ("fixed", None)
    fixed = advisory.NODE_FIXED.get(major)
    if fixed is None:
        return ("unsupported", None)
    return ("vulnerable", fixed) if below(value.strip(), fixed) else ("fixed", None)


def go_verdict(value: str) -> tuple[str, str | None] | None:
    """Classify a Go toolchain version. Minor-only versions mean x.y.0."""
    match = _GO.match(value.strip())
    if not match:
        return None
    line = (int(match.group(1)), int(match.group(2)))
    patch = int(match.group(3) or 0)
    if line > advisory.GO_LAST_AFFECTED_MINOR:
        return ("fixed", None)
    fixed = advisory.GO_FIXED.get(line)
    if fixed is None:
        return ("unsupported", None)
    fixed_patch = int(fixed.rsplit(".", 1)[1])
    return ("vulnerable", fixed) if patch < fixed_patch else ("fixed", None)


def composer_ca_bundle_safe(value: str) -> bool:
    current = parse(value)
    if current is None or len(current) < 2:
        return True
    safe = advisory.COMPOSER_CA_BUNDLE_SAFE.get((current[0], current[1]))
    if safe is not None:
        return not below(value, safe)
    return not below(value, advisory.COMPOSER_CA_BUNDLE_TARGET)

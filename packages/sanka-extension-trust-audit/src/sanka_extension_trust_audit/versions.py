# SPDX-License-Identifier: Apache-2.0
"""Version rules for runtimes and libraries that ship their own root store."""

from __future__ import annotations

import re

from . import advisory

_EXACT = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_JAVA_DOTTED = re.compile(r"^(\d+(?:\.\d+)+)")
_JAVA_8 = re.compile(r"^8u(\d+)")
_CLAUSE = re.compile(r"^(===|==|~=|<=|>=|!=|<|>|\^|~)?\s*v?(\d+(?:\.\d+)*)(\.\*)?$")
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


def java_fixed(tag: str) -> bool | None:
    """Whether a JDK image tag is past the OpenJDK fix; None for floating tags."""
    if match := _JAVA_8.match(tag):
        version: tuple[int, ...] = (8, 0, int(match.group(1)))
    elif match := _JAVA_DOTTED.match(tag):
        version = tuple(int(part) for part in match.group(1).split("."))
    else:
        return None
    if version[0] > advisory.JAVA_LAST_AFFECTED_MAJOR:
        return True
    last = advisory.JAVA_LAST_AFFECTED.get(version[0])
    return last is not None and version > last


def composer_ca_bundle_safe(value: str) -> bool:
    current = parse(value)
    if current is None or len(current) < 2:
        return True
    safe = advisory.COMPOSER_CA_BUNDLE_SAFE.get((current[0], current[1]))
    if safe is not None:
        return not below(value, safe)
    return not below(value, advisory.COMPOSER_CA_BUNDLE_TARGET)


def excludes(spec: str, safe: str) -> bool:
    """True when a PEP 440 or Poetry constraint admits no release at or after safe."""
    minimum = parse(safe)
    assert minimum is not None
    for clause in spec.split(","):
        match = _CLAUSE.match(clause.strip())
        if not match:
            continue
        operator, raw, wildcard = match.group(1) or "==", match.group(2), match.group(3)
        version = tuple(int(part) for part in raw.split("."))
        if operator in {"==", "==="} and wildcard:
            upper: tuple[int, ...] | None = (*version[:-1], version[-1] + 1)
        elif operator in {"==", "==="}:
            upper = None
            if version < minimum:
                return True
        elif operator == "~=" and len(version) > 1:
            upper = (*version[:-2], version[-2] + 1)
        elif operator == "~":
            upper = (version[0], version[1] + 1) if len(version) > 1 else (version[0] + 1,)
        elif operator == "^":
            index = next((i for i, part in enumerate(version) if part), len(version) - 1)
            upper = (*version[:index], version[index] + 1)
        elif operator == "<":
            upper = version
        elif operator == "<=" and version < minimum:
            return True
        else:
            upper = None
        if upper is not None and upper <= minimum:
            return True
    return False

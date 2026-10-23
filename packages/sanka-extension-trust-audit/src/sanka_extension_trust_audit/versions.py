# SPDX-License-Identifier: Apache-2.0
"""Version rules for runtimes and libraries that ship their own root store.

Each rule is decided per advisory and then combined: a release is fixed only when
every advisory accepts it, the highest fixed release wins, and the result names the
advisories that still flag the version.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .advisory import DEFAULT, Advisory, AdvisorySet, ComposerFix

_EXACT = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_JAVA_DOTTED = re.compile(r"^(\d+(?:\.\d+)+)")
_JAVA_8 = re.compile(r"^8u(\d+)")
_CLAUSE = re.compile(r"^(===|==|~=|<=|>=|!=|<|>|\^|~)?\s*v?(\d+(?:\.\d+)*)(\.\*)?$")
_GO = re.compile(r"^(?:go)?(\d+)\.(\d+)(?:\.(\d+))?$")


@dataclass(frozen=True)
class Requirement:
    """status is "fixed", "vulnerable" (upgrade to target) or "unsupported" (no fix)."""

    status: str
    target: str | None
    advisories: tuple[Advisory, ...] = ()


def parse(value: str) -> tuple[int, ...] | None:
    parts = value.strip().lstrip("v").split(".")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def below(value: str, safe: str) -> bool:
    current, minimum = parse(value), parse(safe)
    return current is not None and minimum is not None and current < minimum


def _order(value: str) -> tuple[int, ...]:
    return parse(value) or ()


def _combine(verdicts: list[tuple[str, str | None, Advisory]]) -> Requirement:
    flagged = [verdict for verdict in verdicts if verdict[0] != "fixed"]
    unsupported = tuple(advisory for status, _, advisory in flagged if status == "unsupported")
    if unsupported:
        return Requirement("unsupported", None, unsupported)
    if not flagged:
        return Requirement("fixed", None)
    target = max((target for _, target, _ in flagged if target), key=_order)
    return Requirement("vulnerable", target, tuple(advisory for _, _, advisory in flagged))


def node_requirement(value: str, advisories: AdvisorySet = DEFAULT) -> Requirement | None:
    """Classify a Node.js version against every advisory that lists Node.js fixes."""
    match = _EXACT.match(value.strip())
    if not match:
        return None
    major = int(match.group(1))
    verdicts: list[tuple[str, str | None, Advisory]] = []
    for advisory in advisories.advisories:
        if advisory.node is None:
            continue
        fixed = advisory.node.fixed.get(major)
        if major > advisory.node.last_affected_major:
            verdicts.append(("fixed", None, advisory))
        elif fixed is None:
            verdicts.append(("unsupported", None, advisory))
        elif below(value.strip(), fixed):
            verdicts.append(("vulnerable", fixed, advisory))
        else:
            verdicts.append(("fixed", None, advisory))
    return _combine(verdicts)


def go_requirement(value: str, advisories: AdvisorySet = DEFAULT) -> Requirement | None:
    """Classify a Go toolchain version. Minor-only versions mean x.y.0."""
    match = _GO.match(value.strip())
    if not match:
        return None
    line = (int(match.group(1)), int(match.group(2)))
    patch = int(match.group(3) or 0)
    verdicts: list[tuple[str, str | None, Advisory]] = []
    for advisory in advisories.advisories:
        if advisory.go is None:
            continue
        fixed = advisory.go.fixed.get(line)
        if line > advisory.go.last_affected:
            verdicts.append(("fixed", None, advisory))
        elif fixed is None:
            verdicts.append(("unsupported", None, advisory))
        elif patch < int(fixed.rsplit(".", 1)[1]):
            verdicts.append(("vulnerable", fixed, advisory))
        else:
            verdicts.append(("fixed", None, advisory))
    return _combine(verdicts)


def node_verdict(value: str, advisories: AdvisorySet = DEFAULT) -> tuple[str, str | None] | None:
    """("fixed"|"vulnerable"|"unsupported", target) for a Node.js version."""
    requirement = node_requirement(value, advisories)
    return None if requirement is None else (requirement.status, requirement.target)


def go_verdict(value: str, advisories: AdvisorySet = DEFAULT) -> tuple[str, str | None] | None:
    """("fixed"|"vulnerable"|"unsupported", target) for a Go toolchain version."""
    requirement = go_requirement(value, advisories)
    return None if requirement is None else (requirement.status, requirement.target)


def fixed_releases(kind: str, advisories: AdvisorySet = DEFAULT) -> list[str]:
    """The highest fixed release of each Node.js or Go line across the advisories."""
    best: dict[Any, str] = {}
    for advisory in advisories.advisories:
        fix = advisory.node if kind == "node" else advisory.go
        for line, version in (fix.fixed if fix is not None else {}).items():
            if line not in best or below(best[line], version):
                best[line] = version
    return [best[line] for line in sorted(best)]


def affects(kind: str, advisories: AdvisorySet = DEFAULT) -> tuple[Advisory, ...]:
    """The advisories that list fixes for a runtime: node, go, java or images."""
    attribute = {"node": "node", "go": "go", "java": "java", "images": "images_fixed_after"}
    return tuple(
        advisory
        for advisory in advisories.advisories
        if getattr(advisory, attribute[kind]) is not None
    )


def java_requirement(tag: str, advisories: AdvisorySet = DEFAULT) -> tuple[Advisory, ...] | None:
    """The advisories a JDK image tag is still affected by; None for floating tags."""
    if match := _JAVA_8.match(tag):
        version: tuple[int, ...] = (8, 0, int(match.group(1)))
    elif match := _JAVA_DOTTED.match(tag):
        version = tuple(int(part) for part in match.group(1).split("."))
    else:
        return None
    flagged = []
    for advisory in affects("java", advisories):
        assert advisory.java is not None
        last = advisory.java.last_affected.get(version[0])
        if version[0] <= advisory.java.last_affected_major and (last is None or version <= last):
            flagged.append(advisory)
    return tuple(flagged)


def java_fixed(tag: str, advisories: AdvisorySet = DEFAULT) -> bool | None:
    """Whether a JDK image tag is past every OpenJDK fix; None for floating tags."""
    flagged = java_requirement(tag, advisories)
    return None if flagged is None else not flagged


def library_requirement(
    attribute: str, flags: Callable[[str], bool], advisories: AdvisorySet = DEFAULT
) -> Requirement:
    """A bundled-root library (certifi, webpki_roots) that flags(safe) says is too old."""
    verdicts: list[tuple[str, str | None, Advisory]] = []
    for advisory in advisories.advisories:
        safe = getattr(advisory, attribute)
        if safe is not None and flags(safe):
            verdicts.append(("vulnerable", safe, advisory))
        elif safe is not None:
            verdicts.append(("fixed", None, advisory))
    return _combine(verdicts)


def composer_requirement(
    value: str, advisories: AdvisorySet = DEFAULT
) -> tuple[str, tuple[Advisory, ...]] | None:
    """The upgrade instruction and flagging advisories for a composer/ca-bundle version."""
    current = parse(value)
    if current is None or len(current) < 2:
        return None
    flagged: list[tuple[ComposerFix, Advisory]] = []
    for advisory in advisories.advisories:
        fix = advisory.composer_ca_bundle
        if fix is not None and below(value, fix.lines.get((current[0], current[1]), fix.target)):
            flagged.append((fix, advisory))
    if not flagged:
        return None
    strictest, _ = max(flagged, key=lambda item: _order(item[0].target))
    lines = ", ".join(
        f"or {fixed} on the {line[0]}.{line[1]} line" for line, fixed in strictest.lines.items()
    )
    action = f"Run composer update composer/ca-bundle to reach {strictest.target}"
    return action + (f" ({lines})" if lines else "") + ".", tuple(a for _, a in flagged)


def composer_ca_bundle_safe(value: str, advisories: AdvisorySet = DEFAULT) -> bool:
    return composer_requirement(value, advisories) is None


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

# SPDX-License-Identifier: Apache-2.0
"""Advisories the audit checks, read from the JSON files under advisories/.

Each file records one incident from primary sources: the certificates and keys to
distrust, the roots that root programs withdrew, and the first fixed release of each
runtime or library that ships its own root store. Every file is validated when the
extension starts; an invalid file stops it rather than silently skipping checks.
docs/trust-audit-advisories.md describes how to add one.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, NoReturn

SCHEMA_VERSION = "sanka-trust-advisory/v1"
PENDING_ECOSYSTEMS = frozenset({"bun", "erlang-certifi"})
_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_HEX = re.compile(r"[0-9a-f]{64}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_VERSION = re.compile(r"\d+(?:\.\d+)*")
_EXACT = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_LINE = re.compile(r"(\d+)\.(\d+)")
_JAVA = re.compile(r"8u(\d+)|(\d+(?:\.\d+)*)")


class AdvisoryError(ValueError):
    """An advisory file that does not match the schema."""


@dataclass(frozen=True)
class AffectedCertificate:
    name: str
    sha256: str
    issuer: str
    spki_sha256: str

    @property
    def spki_sha256_b64(self) -> str:
        """The SPKI digest as HPKP and OkHttp pins write it."""
        return base64.b64encode(bytes.fromhex(self.spki_sha256)).decode("ascii")


@dataclass(frozen=True)
class RemovedRoot:
    subject: str
    withdrawn_by: str


@dataclass(frozen=True)
class PlannedRemoval:
    """A root a store dropped for reasons other than the incident: reported as info."""

    subject: str
    removed_by: str
    removed_on: str
    note: str


@dataclass(frozen=True)
class CrossSignRoot:
    subject: str
    title: str
    action: str


@dataclass(frozen=True)
class IssuerRule:
    """Certificates whose issuer organization contains `organization`."""

    organization: str
    rule: str
    title: str  # "{issuer}" stands for the issuing CA's common name
    action: str


@dataclass(frozen=True)
class ComposerFix:
    target: str
    lines: dict[tuple[int, int], str]


@dataclass(frozen=True)
class NodeFix:
    last_affected_major: int
    fixed: dict[int, str]


@dataclass(frozen=True)
class GoFix:
    last_affected: tuple[int, int]
    fixed: dict[tuple[int, int], str]


@dataclass(frozen=True)
class JavaFix:
    last_affected_major: int
    last_affected: dict[int, tuple[int, ...]]
    action: str


@dataclass(frozen=True, eq=False)
class Advisory:
    id: str
    title: str
    url: str
    published: str
    sources: tuple[tuple[str, str], ...]
    trust_label: str
    ca_names: tuple[str, ...]
    verification_risk: str
    certificates: tuple[AffectedCertificate, ...]
    removed_roots: tuple[RemovedRoot, ...]
    planned_removals: tuple[PlannedRemoval, ...]
    affected_parents: tuple[str, ...]
    cross_sign_roots: tuple[CrossSignRoot, ...]
    issuer_organizations: tuple[IssuerRule, ...]
    images_fixed_after: str | None
    certifi: str | None
    webpki_roots: str | None
    composer_ca_bundle: ComposerFix | None
    node: NodeFix | None
    go: GoFix | None
    java: JavaFix | None
    pending: dict[str, str]


class _Reader:
    """Strict field access that names the file and field in every error."""

    def __init__(self, name: str) -> None:
        self.name = name

    def fail(self, where: str, message: str) -> NoReturn:
        raise AdvisoryError(f"{self.name}: {where}: {message}")

    def fields(
        self, value: object, where: str, required: set[str], optional: frozenset[str] = frozenset()
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            self.fail(where, "expected an object")
        if missing := required - value.keys():
            self.fail(where, f"missing {', '.join(sorted(missing))}")
        if unknown := value.keys() - required - optional:
            self.fail(where, f"unknown {', '.join(sorted(unknown))}")
        return value

    def items(self, value: object, where: str) -> list[Any]:
        if not isinstance(value, list):
            self.fail(where, "expected a list")
        return value

    def text(self, value: object, where: str) -> str:
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            self.fail(where, "expected non-empty text without surrounding spaces")
        return value

    def match(self, value: object, where: str, pattern: re.Pattern[str], expected: str) -> str:
        if not isinstance(value, str) or not pattern.fullmatch(value):
            self.fail(where, f"expected {expected}")
        return value

    def url(self, value: object, where: str) -> str:
        text = self.text(value, where)
        if not text.startswith("https://"):
            self.fail(where, "expected an https:// URL")
        return text

    def integer(self, value: object, where: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            self.fail(where, "expected a non-negative integer")
        return value

    def mapping(self, value: object, where: str) -> dict[str, Any]:
        if not isinstance(value, dict) or not value:
            self.fail(where, "expected a non-empty object")
        return value


def _line(reader: _Reader, value: str, where: str) -> tuple[int, int]:
    match = _LINE.fullmatch(value)
    if match is None:
        reader.fail(where, "expected a MAJOR.MINOR release line")
    return int(match.group(1)), int(match.group(2))


def _java_version(reader: _Reader, value: object, where: str) -> tuple[int, ...]:
    text = reader.match(value, where, _JAVA, "a Java version such as 21.0.13 or 8u512")
    match = _JAVA.fullmatch(text)
    assert match is not None
    if match.group(1) is not None:
        return (8, 0, int(match.group(1)))
    return tuple(int(part) for part in text.split("."))


def _release(reader: _Reader, value: object, where: str, line: tuple[int, ...]) -> str:
    """A MAJOR.MINOR.PATCH release that belongs to the given major or major.minor line."""
    release = reader.match(value, where, _EXACT, "MAJOR.MINOR.PATCH")
    if tuple(int(part) for part in release.split(".")[: len(line)]) != line:
        reader.fail(where, "fixed release is on another line")
    return release


def _composer(reader: _Reader, value: object) -> ComposerFix:
    where = "fixes.composer-ca-bundle"
    item = reader.fields(value, where, {"target"}, frozenset({"lines"}))
    lines: dict[tuple[int, int], str] = {}
    for line, fixed in (item.get("lines") or {}).items():
        key = _line(reader, line, f"{where}.lines")
        lines[key] = _release(reader, fixed, f"{where}.lines.{line}", key)
    target = reader.match(item["target"], f"{where}.target", _EXACT, "MAJOR.MINOR.PATCH")
    return ComposerFix(target, lines)


def _node(reader: _Reader, value: object) -> NodeFix:
    item = reader.fields(value, "fixes.node", {"last_affected_major", "fixed"})
    fixed: dict[int, str] = {}
    for major, release in reader.mapping(item["fixed"], "fixes.node.fixed").items():
        where = f"fixes.node.fixed.{major}"
        if not major.isdigit():
            reader.fail(where, "expected a major version key")
        fixed[int(major)] = _release(reader, release, where, (int(major),))
    last = reader.integer(item["last_affected_major"], "fixes.node.last_affected_major")
    return NodeFix(last, fixed)


def _go(reader: _Reader, value: object) -> GoFix:
    item = reader.fields(value, "fixes.go", {"last_affected", "fixed"})
    fixed: dict[tuple[int, int], str] = {}
    for line, release in reader.mapping(item["fixed"], "fixes.go.fixed").items():
        key = _line(reader, line, f"fixes.go.fixed.{line}")
        fixed[key] = _release(reader, release, f"fixes.go.fixed.{line}", key)
    last = reader.text(item["last_affected"], "fixes.go.last_affected")
    return GoFix(_line(reader, last, "fixes.go.last_affected"), fixed)


def _java(reader: _Reader, value: object) -> JavaFix:
    item = reader.fields(value, "fixes.java", {"last_affected_major", "last_affected", "action"})
    last_affected: dict[int, tuple[int, ...]] = {}
    for major, release in reader.mapping(item["last_affected"], "fixes.java.last_affected").items():
        where = f"fixes.java.last_affected.{major}"
        parsed = _java_version(reader, release, where)
        if not major.isdigit() or parsed[0] != int(major):
            reader.fail(where, "release is on another major line")
        last_affected[int(major)] = parsed
    return JavaFix(
        reader.integer(item["last_affected_major"], "fixes.java.last_affected_major"),
        last_affected,
        reader.text(item["action"], "fixes.java.action"),
    )


def _fixes(reader: _Reader, value: object) -> dict[str, Any]:
    fixes = reader.fields(
        value,
        "fixes",
        set(),
        frozenset({"certifi", "webpki-roots", "composer-ca-bundle", "node", "go", "java"}),
    )
    library = {
        attribute: reader.match(fixes[key], f"fixes.{key}", _VERSION, "a version")
        if key in fixes
        else None
        for key, attribute in (("certifi", "certifi"), ("webpki-roots", "webpki_roots"))
    }
    parsers = {"composer-ca-bundle": _composer, "node": _node, "go": _go, "java": _java}
    runtimes = {
        key.replace("-", "_"): parse(reader, fixes[key]) if key in fixes else None
        for key, parse in parsers.items()
    }
    return library | runtimes


def parse_advisory(document: object, name: str) -> Advisory:
    """Validate one advisory document; name labels errors."""
    reader = _Reader(name)
    data = reader.fields(
        document,
        "advisory",
        {
            "schema_version",
            "id",
            "title",
            "url",
            "published",
            "sources",
            "trust_label",
            "ca_names",
            "verification_risk",
            "certificates",
        },
        frozenset(
            {
                "removed_roots",
                "planned_removals",
                "affected_parents",
                "cross_sign_roots",
                "issuer_organizations",
                "images_fixed_after",
                "fixes",
                "pending",
            }
        ),
    )
    if data["schema_version"] != SCHEMA_VERSION:
        reader.fail("schema_version", f"expected {SCHEMA_VERSION}")
    sources = []
    for index, item in enumerate(reader.items(data["sources"], "sources")):
        where = f"sources[{index}]"
        source = reader.fields(item, where, {"title", "url"})
        sources.append((reader.text(source["title"], where), reader.url(source["url"], where)))
    if not sources:
        reader.fail("sources", "list at least one primary source")
    certificates = []
    for index, item in enumerate(reader.items(data["certificates"], "certificates")):
        where = f"certificates[{index}]"
        cert = reader.fields(item, where, {"name", "issuer", "sha256", "spki_sha256"})
        certificates.append(
            AffectedCertificate(
                reader.text(cert["name"], f"{where}.name"),
                reader.match(cert["sha256"], f"{where}.sha256", _HEX, "lowercase SHA-256 hex"),
                reader.text(cert["issuer"], f"{where}.issuer"),
                reader.match(cert["spki_sha256"], f"{where}.spki_sha256", _HEX, "SHA-256 hex"),
            )
        )
    if len({cert.sha256 for cert in certificates}) != len(certificates):
        reader.fail("certificates", "a certificate is listed twice")
    removed_roots = []
    for index, item in enumerate(reader.items(data.get("removed_roots", []), "removed_roots")):
        where = f"removed_roots[{index}]"
        root = reader.fields(item, where, {"subject", "withdrawn_by"})
        removed_roots.append(
            RemovedRoot(
                reader.text(root["subject"], f"{where}.subject"),
                reader.text(root["withdrawn_by"], f"{where}.withdrawn_by"),
            )
        )
    planned = []
    for index, item in enumerate(
        reader.items(data.get("planned_removals", []), "planned_removals")
    ):
        where = f"planned_removals[{index}]"
        root = reader.fields(item, where, {"subject", "removed_by", "removed_on", "note"})
        planned.append(
            PlannedRemoval(
                reader.text(root["subject"], f"{where}.subject"),
                reader.text(root["removed_by"], f"{where}.removed_by"),
                reader.match(root["removed_on"], f"{where}.removed_on", _DATE, "YYYY-MM-DD"),
                reader.text(root["note"], f"{where}.note"),
            )
        )
    cross = []
    for index, item in enumerate(
        reader.items(data.get("cross_sign_roots", []), "cross_sign_roots")
    ):
        where = f"cross_sign_roots[{index}]"
        root = reader.fields(item, where, {"subject", "title", "action"})
        cross.append(
            CrossSignRoot(
                reader.text(root["subject"], f"{where}.subject"),
                reader.text(root["title"], f"{where}.title"),
                reader.text(root["action"], f"{where}.action"),
            )
        )
    issuers = []
    for index, item in enumerate(
        reader.items(data.get("issuer_organizations", []), "issuer_organizations")
    ):
        where = f"issuer_organizations[{index}]"
        rule = reader.fields(item, where, {"organization", "rule", "title", "action"})
        issuers.append(
            IssuerRule(
                reader.text(rule["organization"], f"{where}.organization"),
                reader.match(rule["rule"], f"{where}.rule", _ID, "a lowercase rule id"),
                reader.text(rule["title"], f"{where}.title"),
                reader.text(rule["action"], f"{where}.action"),
            )
        )
    pending = reader.fields(data.get("pending", {}), "pending", set(), PENDING_ECOSYSTEMS)
    images = data.get("images_fixed_after")
    return Advisory(
        id=reader.match(data["id"], "id", _ID, "a lowercase id such as emudhra-2026-10"),
        title=reader.text(data["title"], "title"),
        url=reader.url(data["url"], "url"),
        published=reader.match(data["published"], "published", _DATE, "YYYY-MM-DD"),
        sources=tuple(sources),
        trust_label=reader.text(data["trust_label"], "trust_label"),
        ca_names=tuple(
            reader.text(item, "ca_names") for item in reader.items(data["ca_names"], "ca_names")
        ),
        verification_risk=reader.text(data["verification_risk"], "verification_risk"),
        certificates=tuple(certificates),
        removed_roots=tuple(removed_roots),
        planned_removals=tuple(planned),
        affected_parents=tuple(
            reader.text(item, "affected_parents")
            for item in reader.items(data.get("affected_parents", []), "affected_parents")
        ),
        cross_sign_roots=tuple(cross),
        issuer_organizations=tuple(issuers),
        images_fixed_after=None
        if images is None
        else reader.match(images, "images_fixed_after", _DATE, "YYYY-MM-DD"),
        pending={key: reader.text(value, f"pending.{key}") for key, value in pending.items()},
        **_fixes(reader, data.get("fixes", {})),
    )


def _documents(directory: Path | None) -> list[tuple[str, str]]:
    if directory is None:
        root = resources.files("sanka_extension_trust_audit") / "advisories"
        return sorted(
            (entry.name, entry.read_text(encoding="utf-8"))
            for entry in root.iterdir()
            if entry.name.endswith(".json")
        )
    return sorted(
        (path.name, path.read_text(encoding="utf-8")) for path in directory.glob("*.json")
    )


def load_advisories(directory: Path | None = None) -> tuple[Advisory, ...]:
    """Every advisory in directory (default: the packaged ones), oldest first."""
    advisories = []
    for name, text in _documents(directory):
        try:
            document = json.loads(text)
        except ValueError as exc:
            raise AdvisoryError(f"{name}: not valid JSON") from exc
        advisory = parse_advisory(document, name)
        if name != f"{advisory.id}.json":
            raise AdvisoryError(f"{name}: file name must be {advisory.id}.json")
        advisories.append(advisory)
    if not advisories:
        raise AdvisoryError("No trust advisories found")
    return tuple(sorted(advisories, key=lambda item: (item.published, item.id)))


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


class AdvisorySet:
    """Advisories checked together. A key listed by several keeps the oldest advisory."""

    def __init__(self, advisories: tuple[Advisory, ...]) -> None:
        if not advisories:
            raise ValueError("At least one advisory is required")
        self.advisories = advisories
        self.several = len(advisories) > 1
        self.certificates: dict[str, tuple[AffectedCertificate, Advisory]] = {}
        self.spki: dict[str, tuple[str, Advisory]] = {}
        self.issuer_names: dict[str, Advisory] = {}
        self.removed_roots: dict[str, tuple[RemovedRoot, Advisory]] = {}
        self.planned_removals: dict[str, tuple[PlannedRemoval, Advisory]] = {}
        self.parents: dict[str, Advisory] = {}
        self.cross_sign_roots: dict[str, tuple[CrossSignRoot, Advisory]] = {}
        self.issuer_rules: list[tuple[IssuerRule, Advisory]] = []
        # Normalized needle (base64, or lowercase hex without separators) -> label.
        self.needles: dict[str, tuple[str, Advisory]] = {}
        for advisory in advisories:
            for cert in advisory.certificates:
                self.certificates.setdefault(cert.sha256, (cert, advisory))
                self.spki.setdefault(cert.spki_sha256, (cert.name, advisory))
                self.issuer_names.setdefault(cert.name, advisory)
                self.needles.setdefault(
                    cert.spki_sha256_b64, (f"SPKI pin for {cert.name}", advisory)
                )
            for cert in advisory.certificates:
                self.needles.setdefault(cert.spki_sha256, (f"SPKI digest of {cert.name}", advisory))
            for cert in advisory.certificates:
                label = f"fingerprint of {cert.name} (issuer {cert.issuer})"
                self.needles.setdefault(cert.sha256, (label, advisory))
            for root in advisory.removed_roots:
                self.removed_roots.setdefault(root.subject, (root, advisory))
            for planned in advisory.planned_removals:
                self.planned_removals.setdefault(planned.subject, (planned, advisory))
            for parent in advisory.affected_parents:
                self.parents.setdefault(parent, advisory)
            for cross in advisory.cross_sign_roots:
                self.cross_sign_roots.setdefault(cross.subject, (cross, advisory))
            self.issuer_rules += [(rule, advisory) for rule in advisory.issuer_organizations]

    def attribution(self, advisories: Iterable[Advisory]) -> str | None:
        """The advisory ids a finding names; None while a single advisory is checked."""
        if not self.several:
            return None
        ids = _unique(advisory.id for advisory in advisories)
        return "+".join(ids) if ids else None

    @property
    def id(self) -> str:
        return "+".join(advisory.id for advisory in self.advisories)

    def descriptor(self) -> dict[str, object]:
        """The plan and MANIFEST advisory field; a single advisory keeps its 0.1.0a1 shape."""
        newest = self.advisories[-1]
        if not self.several:
            return {"id": newest.id, "title": newest.title, "url": newest.url}
        return {
            "id": self.id,
            "title": "; ".join(advisory.title for advisory in self.advisories),
            "url": newest.url,
            "includes": [
                {"id": advisory.id, "title": advisory.title, "url": advisory.url}
                for advisory in self.advisories
            ],
        }

    def trust_labels(self) -> str:
        return ", ".join(_unique(advisory.trust_label for advisory in self.advisories))

    def keystore_hint(self) -> str:
        return " or ".join(_unique(name for a in self.advisories for name in a.ca_names))

    def verification_risk(self) -> str:
        return " ".join(_unique(advisory.verification_risk for advisory in self.advisories))


DEFAULT = AdvisorySet(load_advisories())
# Every compromised key of the packaged advisories as a base64 pin, as in 0.1.0a1.
COMPROMISED_SPKI: dict[str, str] = {
    base64.b64encode(bytes.fromhex(digest)).decode("ascii"): name
    for digest, (name, _) in DEFAULT.spki.items()
}

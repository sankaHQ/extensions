# SPDX-License-Identifier: Apache-2.0
"""Static inventory of the places a repository decides which TLS roots to trust.

The scanner reads files only. It never runs package managers, builds images or
opens network connections, so it cannot see deployed binaries, images that were
already built or the trust store of a running host.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

from . import advisory, versions
from .certs import Certificate, read_binary, read_pem

SEVERITIES = ("critical", "high", "medium", "info")
PATCH_MANIFEST_SCHEMA = "sanka-trust-patches/v1"
MAX_TEXT_BYTES = 1 << 20
MAX_CERT_BYTES = 8 << 20
MAX_PER_RULE_AND_FILE = 20

SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".sanka",
        ".venv",
        "venv",
        ".tox",
        ".nox",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".gradle",
        ".terraform",
        ".idea",
        ".next",
        ".nuxt",
        ".turbo",
        ".svelte-kit",
        ".parcel-cache",
        ".cache",
        ".expo",
        ".dart_tool",
        "coverage",
        "storybook-static",
        "target",
    }
)
TEST_PARTS = frozenset(
    {"test", "tests", "spec", "specs", "__tests__", "testdata", "fixtures", "e2e", "testing"}
)
SOURCE_SUFFIXES = frozenset(
    {
        ".py",
        ".go",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".mjs",
        ".cjs",
        ".java",
        ".kt",
        ".kts",
        ".scala",
        ".groovy",
        ".gradle",
        ".rb",
        ".php",
        ".cs",
        ".sh",
        ".bash",
        ".zsh",
        ".ps1",
        ".yml",
        ".yaml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".env",
        ".properties",
        ".xml",
        ".tf",
        ".hcl",
        ".mk",
    }
)
SOURCE_NAMES = frozenset(
    {"Makefile", "Jenkinsfile", "Procfile", ".npmrc", ".yarnrc", ".yarnrc.yml", ".env"}
)
PIN_ONLY_SUFFIXES = frozenset({".json", ".plist"})
CERT_SUFFIXES = frozenset(
    {
        ".pem",
        ".crt",
        ".cer",
        ".der",
        ".ca-bundle",
        ".p7b",
        ".p7c",
        ".jks",
        ".keystore",
        ".truststore",
        ".p12",
        ".pfx",
        ".bks",
    }
)
CERT_NAME = re.compile(r"^cacerts$", re.I)
DOCKERFILE = re.compile(r"^(Dockerfile(\..+)?|.+\.Dockerfile|Containerfile)$")
REQUIREMENTS = re.compile(r"^(requirements|constraints)[^/]*\.(txt|in)$", re.I)
CI_FILE = re.compile(
    r"^(\.github/workflows/[^/]+\.ya?ml|\.gitlab-ci\.yml|\.circleci/config\.yml"
    r"|azure-pipelines\.yml|bitbucket-pipelines\.yml|(.*/)?(docker-)?compose[^/]*\.ya?ml)$"
)
JAVA_IMAGES = frozenset(
    {
        "eclipse-temurin",
        "openjdk",
        "amazoncorretto",
        "zulu-openjdk",
        "ibm-semeru-runtimes",
        "sapmachine",
        "liberica-openjdk-debian",
        "liberica-openjdk-alpine",
        "maven",
        "gradle",
        "tomcat",
        "jetty",
    }
)
JAVA_ACTION = (
    "Remove the root from every JDK this code runs on until your JDK vendor ships a fix, "
    "for example in the image build: keytool -delete -cacerts -storepass changeit "
    f'-alias "{advisory.JAVA_ROOT_ALIAS}". Aliases differ between vendors; list them with '
    "keytool -list -cacerts | grep -i emsign."
)

# Patterns that switch TLS certificate verification off. The incident lets
# anyone mint a certificate that chains to a compromised CA, and broken chains
# after the root removal push teams toward exactly these workarounds.
_ANY = (
    r"\bcurl\b[^\n]*\s(?:-[a-zA-Z]*k[a-zA-Z]*|--insecure)\b",
    r"\bwget\b[^\n]*--no-check-certificate",
    r"GIT_SSL_NO_VERIFY\s*[=:]\s*['\"]?(?:1|true)",
    r"http\.sslVerify\s*(?:=\s*|\s+)['\"]?false",
    r"NODE_TLS_REJECT_UNAUTHORIZED\s*[=:]\s*['\"]?0",
    r"PYTHONHTTPSVERIFY\s*[=:]\s*['\"]?0",
    r"strict-ssl\s*[=:\s]\s*['\"]?false",
)
_BY_SUFFIX = {
    ".py": (
        r"\bverify\s*=\s*False\b",
        r"ssl\._create_unverified_context",
        r"\bcheck_hostname\s*=\s*False\b",
        r"\bCERT_NONE\b",
    ),
    ".go": (r"InsecureSkipVerify\s*:\s*true",),
    ".js": (r"rejectUnauthorized\s*:\s*false",),
    ".java": (
        r"\bNoopHostnameVerifier\b",
        r"ALLOW_ALL_HOSTNAME_VERIFIER",
        r"\bTrustAllStrategy\b",
        r"\bInsecureTrustManagerFactory\b",
        r"\bTrustAll(?:Certs|Manager|TrustManager)\b",
    ),
    ".rb": (r"\bVERIFY_NONE\b",),
    ".php": (
        r"CURLOPT_SSL_VERIFYPEER\s*,\s*(?:false|0)\b",
        r"['\"]verify['\"]\s*=>\s*false\b",
    ),
    ".cs": (
        r"DangerousAcceptAnyServerCertificateValidator",
        r"ValidationCallback\s*\+?=\s*\([^)]*\)\s*=>\s*true\b",
    ),
}
for _alias, _base in {
    ".jsx": ".js",
    ".ts": ".js",
    ".tsx": ".js",
    ".mjs": ".js",
    ".cjs": ".js",
    ".kt": ".java",
    ".kts": ".java",
    ".scala": ".java",
    ".groovy": ".java",
    ".gradle": ".java",
}.items():
    _BY_SUFFIX[_alias] = _BY_SUFFIX[_base]
_ANY_RE = re.compile("|".join(_ANY))
_SUFFIX_RE = {suffix: re.compile("|".join(patterns)) for suffix, patterns in _BY_SUFFIX.items()}
_CUSTOM_CA = re.compile(
    r"\b(REQUESTS_CA_BUNDLE|SSL_CERT_FILE|SSL_CERT_DIR|CURL_CA_BUNDLE|NODE_EXTRA_CA_CERTS"
    r"|GIT_SSL_CAINFO|javax\.net\.ssl\.trustStore)\b"
)
_LOCK_PACKAGE = re.compile(
    r'^\[\[package\]\]\s*\nname\s*=\s*"([^"]+)"\s*\nversion\s*=\s*"([^"]+)"', re.M
)
_FROM = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", re.I)
_CI_VERSION = re.compile(r"^(\s*-?\s*(node-version|go-version)\s*:\s*['\"]?)([^'\"\s#]+)")
_CI_JAVA = re.compile(r"^\s*-?\s*java-version\s*:")
_CI_IMAGE = re.compile(r"^(\s*-?\s*image\s*:\s*['\"]?)([^'\"\s#]+)")
_REQ_CERTIFI = re.compile(r"^(\s*certifi\s*==\s*)([0-9][0-9.]*)(.*)$", re.I)
_REQ_CERTIFI_CAP = re.compile(r"^\s*certifi\b[^#]*?(<=?)\s*([0-9][0-9.]*)", re.I)
_PYPROJECT_CERTIFI = re.compile(r"""(["']certifi\s*==\s*)([0-9][0-9.]*)(["'])""", re.I)
_POETRY_CERTIFI = re.compile(r"""^(\s*certifi\s*=\s*["'](?:==)?)([0-9][0-9.]*)(["'])""", re.I)
_PACKAGE_NODE = re.compile(r'^(\s*"node"\s*:\s*"v?)(\d+\.\d+\.\d+)(")')
_GO_DIRECTIVE = re.compile(r"^go\s+(\S+)\s*$")
_GO_TOOLCHAIN = re.compile(r"^(toolchain\s+go)(\S+)\s*$")
_TOOL_VERSIONS = re.compile(r"^(\s*(nodejs|golang|java|bun)\s+)(\S+)")


@dataclass(frozen=True)
class Edit:
    """Replace lines start..end (1-based, inclusive). start == end + 1 inserts."""

    path: str
    start: int
    end: int
    lines: tuple[str, ...]


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    path: str
    line: int | None
    title: str
    action: str
    evidence: str = ""
    edit: Edit | None = None

    def as_json(self) -> dict[str, object]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "path": self.path,
            "line": self.line,
            "title": self.title,
            "action": self.action,
            "evidence": self.evidence,
            "patched": self.edit is not None,
        }


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    files_scanned: int = 0
    truncated: bool = False

    def counts(self) -> dict[str, int]:
        return {level: sum(f.severity == level for f in self.findings) for level in SEVERITIES}


def is_patch_output(directory: Path) -> bool:
    """True for a directory that a previous apply generated."""
    manifest = directory / "MANIFEST.json"
    try:
        if manifest.is_symlink() or not manifest.is_file():
            return False
        if manifest.stat().st_size > MAX_CERT_BYTES:
            return False
        document = json.loads(manifest.read_bytes())
    except (OSError, ValueError):
        return False
    return isinstance(document, dict) and document.get("schema_version") == PATCH_MANIFEST_SCHEMA


def walk(root: Path) -> Iterator[tuple[str, Path]]:
    """Yield (posix relative path, absolute path) for regular files, sorted."""
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(current)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in SKIP_DIRS
            and not (base / name).is_symlink()
            # Nested repositories and worktrees are separate projects.
            and not (base / name / ".git").exists()
            and not is_patch_output(base / name)
        )
        for name in sorted(filenames):
            path = base / name
            if path.is_symlink() or not path.is_file():
                continue
            yield path.relative_to(root).as_posix(), path


def _is_test(rel: str) -> bool:
    pure = PurePosixPath(rel)
    name = pure.name
    return (
        bool(TEST_PARTS.intersection(pure.parts[:-1]))
        or name.startswith("test_")
        or name.endswith(("_test.go", "_test.py"))
        or ".test." in name
        or ".spec." in name
    )


def _evidence(line: str) -> str:
    text = line.strip()
    return text if len(text) <= 160 else text[:157] + "..."


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _compromised_needles() -> dict[str, str]:
    """Normalized needle -> label. Hex is lowercase without separators."""
    needles: dict[str, str] = {}
    for b64, name in advisory.COMPROMISED_SPKI.items():
        needles[b64] = f"SPKI pin for {name}"
    for hex_digest, name in advisory.COMPROMISED_SPKI_HEX.items():
        needles[hex_digest] = f"SPKI digest of {name}"
    for cert in advisory.AFFECTED_CERTIFICATES:
        needles[cert.sha256] = f"fingerprint of {cert.name} (issuer {cert.issuer})"
    return needles


_NEEDLES = _compromised_needles()
# Candidate tokens: base64 SHA-256, bare hex SHA-256, colon-separated hex.
_DIGEST_TOKEN = re.compile(
    r"[A-Za-z0-9+/]{43}="
    r"|(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])"
    r"|(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])"
)
_AFFECTED_SHA = {c.sha256: c for c in advisory.AFFECTED_CERTIFICATES}
_AFFECTED_NAMES = frozenset(c.name for c in advisory.AFFECTED_CERTIFICATES)


class _Scan:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.report = Report()
        self.java_evidence: tuple[str, int | None, str] | None = None
        self.bun_evidence: tuple[str, int | None, str] | None = None
        self._per_rule_file: dict[tuple[str, str], int] = {}

    def add(self, finding: Finding) -> None:
        # Test code and fixtures are rarely deployed: keep them visible, lower.
        if finding.severity in {"critical", "high"} and _is_test(finding.path):
            finding = replace(finding, severity="medium")
        key = (finding.rule, finding.path)
        seen = self._per_rule_file.get(key, 0)
        if seen >= MAX_PER_RULE_AND_FILE:
            self.report.truncated = True
            return
        self._per_rule_file[key] = seen + 1
        self.report.findings.append(finding)

    def run(self) -> Report:
        for rel, path in walk(self.root):
            self.report.files_scanned += 1
            try:
                self.file(rel, path)
            except (OSError, ValueError, IndexError, RecursionError):
                # One unreadable or malformed file must not fail the whole scan.
                continue
        if self.java_evidence is not None:
            rel, line, evidence = self.java_evidence
            self.add(
                Finding(
                    "java-cacerts",
                    "medium",
                    rel,
                    line,
                    "Java runtimes still trust emSign ECC Root CA - G3",
                    f"{advisory.PENDING['java']} {JAVA_ACTION}",
                    evidence,
                )
            )
        if self.bun_evidence is not None:
            rel, line, evidence = self.bun_evidence
            self.add(
                Finding(
                    "bun-pending",
                    "medium",
                    rel,
                    line,
                    "Bun has no fixed release yet",
                    f"{advisory.PENDING['bun']} Watch for a Bun release that drops emSign ECC "
                    "Root CA - G3; until then avoid Bun for clients on untrusted networks.",
                    evidence,
                )
            )
        self.report.findings.sort(
            key=lambda f: (SEVERITIES.index(f.severity), f.path, f.line or 0, f.rule)
        )
        return self.report

    # -- dispatch -----------------------------------------------------------------

    def file(self, rel: str, path: Path) -> None:
        name = path.name
        suffix = path.suffix.lower()
        size = path.stat().st_size
        if suffix in CERT_SUFFIXES or CERT_NAME.match(name):
            if size <= MAX_CERT_BYTES:
                self.certificates(rel, path.read_bytes(), editable=True)
            return
        if size > MAX_TEXT_BYTES:
            return
        raw = path.read_bytes()
        if b"\x00" in raw[:8192]:
            return
        text = raw.decode("utf-8", "replace")
        if b"-----BEGIN" in raw and b"CERTIFICATE-----" in raw:
            self.certificates(rel, raw, editable=False)
        if name in {"bun.lockb", "bun.lock", "bunfig.toml"} and self.bun_evidence is None:
            self.bun_evidence = (rel, None, name)
        if REQUIREMENTS.match(name):
            self.requirements(rel, text)
        elif name == "pyproject.toml":
            self.pyproject(rel, text)
        elif name in {"poetry.lock", "uv.lock", "pdm.lock", "Cargo.lock"}:
            self.package_lock(rel, name, text)
        elif name == "Pipfile.lock":
            self.pipfile_lock(rel, text)
        elif name == "composer.lock":
            self.composer_lock(rel, text)
        elif name in {"mix.lock", "rebar.lock"}:
            self.erlang_lock(rel, text)
        elif name in {".nvmrc", ".node-version"}:
            self.version_file(rel, text, "node")
        elif name == ".go-version":
            self.version_file(rel, text, "go")
        elif name == ".tool-versions":
            self.tool_versions(rel, text)
        elif name == "package.json":
            self.package_json(rel, text)
        elif name == "go.mod":
            self.go_mod(rel, text)
        elif (
            name in {"pom.xml", "build.gradle", "build.gradle.kts", ".java-version"}
            and "com.android" not in text
            and self.java_evidence is None
        ):
            self.java_evidence = (rel, None, name)
        if DOCKERFILE.match(name):
            self.dockerfile(rel, text)
        if CI_FILE.match(rel):
            self.ci(rel, text)
        if suffix in SOURCE_SUFFIXES or name in SOURCE_NAMES or DOCKERFILE.match(name):
            self.source(rel, suffix, text)
            self.pins(rel, text)
        elif suffix in PIN_ONLY_SUFFIXES:
            self.pins(rel, text)

    # -- certificates -------------------------------------------------------------

    def certificates(self, rel: str, raw: bytes, editable: bool) -> None:
        is_pem = b"-----BEGIN" in raw
        certificates = read_pem(raw) if is_pem else read_binary(raw)
        if not certificates:
            if rel.lower().endswith((".p12", ".pfx", ".jks", ".keystore", ".bks")):
                self.add(
                    Finding(
                        "keystore-unreadable",
                        "info",
                        rel,
                        None,
                        "Keystore could not be inspected",
                        "It may be encrypted. List it with keytool -list -v and look for "
                        "emSign or eMudhra entries.",
                    )
                )
            return
        lines = raw.decode("utf-8", "replace").split("\n") if is_pem else []
        # A chain for a certificate the compromised CA issued needs a new
        # certificate, not an edited chain file.
        issued = any(
            c.issuer_cn in _AFFECTED_NAMES and c.sha256 not in _AFFECTED_SHA for c in certificates
        )
        # PEM embedded in source code is reported, never cut out of the source.
        removable = editable and is_pem and len(certificates) > 1 and not issued
        for certificate in certificates:
            self.certificate(rel, certificate, lines, removable)

    def certificate(self, rel: str, cert: Certificate, lines: list[str], removable: bool) -> None:
        line = cert.lines[0] if cert.lines else None
        evidence = f"subject: {cert.subject_cn}; issuer: {cert.issuer_cn}"
        if cert.sha256 in _AFFECTED_SHA or cert.spki_sha256 in advisory.COMPROMISED_SPKI_HEX:
            name = advisory.COMPROMISED_SPKI_HEX.get(cert.spki_sha256, cert.subject_cn)
            self.add(
                Finding(
                    "compromised-ca-certificate",
                    "critical",
                    rel,
                    line,
                    f"Compromised CA certificate {name} is in this repository",
                    "Remove it from every bundle and chain. If a server presents this chain, "
                    "replace its certificate with one from another CA now.",
                    evidence,
                    self._removal(rel, cert, lines) if removable else None,
                )
            )
        elif cert.issuer_cn in _AFFECTED_NAMES:
            self.add(
                Finding(
                    "issued-by-compromised-ca",
                    "critical",
                    rel,
                    line,
                    f"Certificate issued by compromised CA {cert.issuer_cn}",
                    "Anyone can forge certificates from this CA. Replace this certificate "
                    "with one from another CA and revoke it.",
                    evidence,
                )
            )
        elif cert.subject_cn in advisory.REMOVED_ROOTS and cert.self_issued:
            self.add(
                Finding(
                    "removed-root-in-bundle",
                    "high",
                    rel,
                    line,
                    f"Bundle still trusts {cert.subject_cn}",
                    "Mozilla and Chrome withdrew trust in this root. Remove it from the bundle "
                    "or regenerate the bundle from a current certifi or ca-certificates release.",
                    evidence,
                    self._removal(rel, cert, lines) if removable else None,
                )
            )
        elif cert.subject_cn in advisory.AFFECTED_PARENTS:
            self.add(
                Finding(
                    "affected-parent-in-bundle",
                    "high",
                    rel,
                    line,
                    f"Bundle trusts {cert.subject_cn}, the issuer of the compromised CAs",
                    "Remove it from the bundle.",
                    evidence,
                    self._removal(rel, cert, lines) if removable else None,
                )
            )
        elif cert.subject_cn == advisory.CROSS_SIGN_ROOT and cert.self_issued:
            self.add(
                Finding(
                    "cross-sign-root-in-bundle",
                    "medium",
                    rel,
                    line,
                    "Bundle trusts AAA Certificate Services, root of Sectigo's cross-certificates",
                    "Sectigo revoked the cross-certificates for the compromised keys on "
                    "2026-10-19, but clients that skip revocation checks still accept them. "
                    "Remove this root unless something you call still needs it.",
                    evidence,
                )
            )
        elif "eMudhra" in cert.issuer_o and not cert.self_issued:
            self.add(
                Finding(
                    "emudhra-issued-certificate",
                    "medium",
                    rel,
                    line,
                    f"Certificate issued by eMudhra CA {cert.issuer_cn}",
                    "Confirm it does not chain to emSign ECC Root CA - G3. If it does, "
                    "browsers and updated clients will reject it: replace it.",
                    evidence,
                )
            )

    def _removal(self, rel: str, cert: Certificate, lines: list[str]) -> Edit | None:
        if cert.lines is None:
            return None
        start, end = cert.lines
        while start > 1 and lines[start - 2].startswith("#"):
            start -= 1
        if end < len(lines) and not lines[end].strip():
            end += 1
        return Edit(rel, start, end, ())

    # -- Python ---------------------------------------------------------------

    def _certifi(self, rel: str, number: int, line: str, version: str, edit: Edit | None) -> None:
        self.add(
            Finding(
                "certifi-version",
                "high",
                rel,
                number,
                f"certifi {version} still trusts emSign ECC Root CA - G3",
                f"Upgrade certifi to {advisory.CERTIFI_TARGET} or later.",
                _evidence(line),
                edit,
            )
        )

    def requirements(self, rel: str, text: str) -> None:
        hashed = "--hash" in text
        for number, line in enumerate(text.split("\n"), 1):
            match = _REQ_CERTIFI.match(line)
            if match and versions.below(match.group(2), advisory.CERTIFI_SAFE):
                edit = None
                if not hashed:
                    new = f"{match.group(1)}{advisory.CERTIFI_TARGET}{match.group(3)}"
                    edit = Edit(rel, number, number, (new,))
                self._certifi(rel, number, line, match.group(2), edit)
                continue
            cap = _REQ_CERTIFI_CAP.match(line)
            if cap:
                operator, bound = cap.groups()
                safe = advisory.CERTIFI_SAFE
                if versions.below(bound, safe) or (
                    operator == "<" and not versions.below(safe, bound)
                ):
                    self._certifi(rel, number, line, f"{operator}{bound}", None)

    def pyproject(self, rel: str, text: str) -> None:
        for number, line in enumerate(text.split("\n"), 1):
            for pattern in (_PYPROJECT_CERTIFI, _POETRY_CERTIFI):
                match = pattern.search(line)
                if match and versions.below(match.group(2), advisory.CERTIFI_SAFE):
                    new = (
                        line[: match.start()]
                        + match.group(1)
                        + advisory.CERTIFI_TARGET
                        + match.group(3)
                        + line[match.end() :]
                    )
                    edit = Edit(rel, number, number, (new,))
                    self._certifi(rel, number, line, match.group(2), edit)
                    break

    def package_lock(self, rel: str, name: str, text: str) -> None:
        packages = [(m.group(1), m.group(2), m.start()) for m in _LOCK_PACKAGE.finditer(text)]
        if name == "Cargo.lock":
            for crate in ("webpki-roots", "webpki-root-certs"):
                entries = [(v, o) for n, v, o in packages if n == crate]
                if any(not versions.below(v, advisory.WEBPKI_SAFE) for v, _ in entries):
                    continue  # 0.26.x releases are shims over the fixed 1.x crate.
                for version, offset in entries:
                    if versions.below(version, advisory.WEBPKI_SAFE):
                        self.add(
                            Finding(
                                "webpki-roots-version",
                                "high",
                                rel,
                                _line_of(text, offset) + 2,
                                f"{crate} {version} still trusts emSign ECC Root CA - G3",
                                f"Run cargo update -p {crate} to reach {advisory.WEBPKI_SAFE} "
                                "or later, then rebuild and redeploy.",
                                f'name = "{crate}" version = "{version}"',
                            )
                        )
            return
        command = {
            "poetry.lock": "poetry update certifi",
            "uv.lock": "uv lock --upgrade-package certifi",
            "pdm.lock": "pdm update certifi",
        }[name]
        for package, version, offset in packages:
            if package == "certifi" and versions.below(version, advisory.CERTIFI_SAFE):
                self.add(
                    Finding(
                        "certifi-version",
                        "high",
                        rel,
                        _line_of(text, offset) + 2,
                        f"certifi {version} still trusts emSign ECC Root CA - G3",
                        f"Run {command} to reach {advisory.CERTIFI_TARGET} or later.",
                        f'name = "certifi" version = "{version}"',
                    )
                )

    def pipfile_lock(self, rel: str, text: str) -> None:
        data = json.loads(text)
        for section in ("default", "develop"):
            entry = data.get(section, {}).get("certifi") if isinstance(data, dict) else None
            version = entry.get("version", "") if isinstance(entry, dict) else ""
            if isinstance(version, str) and versions.below(
                version.lstrip("="), advisory.CERTIFI_SAFE
            ):
                self.add(
                    Finding(
                        "certifi-version",
                        "high",
                        rel,
                        _line_of(text, max(text.find('"certifi"'), 0)),
                        f"certifi {version.lstrip('=')} still trusts emSign ECC Root CA - G3",
                        f"Run pipenv update certifi to reach {advisory.CERTIFI_TARGET} or later.",
                        f'"certifi": "{version}"',
                    )
                )

    # -- PHP, Erlang ------------------------------------------------------------

    def composer_lock(self, rel: str, text: str) -> None:
        data = json.loads(text)
        if not isinstance(data, dict):
            return
        for section in ("packages", "packages-dev"):
            for package in data.get(section) or []:
                if not isinstance(package, dict) or package.get("name") != "composer/ca-bundle":
                    continue
                version = str(package.get("version", ""))
                if not versions.composer_ca_bundle_safe(version):
                    self.add(
                        Finding(
                            "composer-ca-bundle-version",
                            "high",
                            rel,
                            _line_of(text, max(text.find('"composer/ca-bundle"'), 0)),
                            f"composer/ca-bundle {version} still trusts the compromised CAs",
                            "Run composer update composer/ca-bundle to reach 1.6.0 "
                            "(or 1.4.16 on the 1.4 line).",
                            f'"composer/ca-bundle": "{version}"',
                        )
                    )

    def erlang_lock(self, rel: str, text: str) -> None:
        match = re.search(r'"certifi":\s*\{:hex,\s*:certifi,\s*"([^"]+)"|<<"certifi">>', text)
        if match:
            self.add(
                Finding(
                    "erlang-certifi-pending",
                    "medium",
                    rel,
                    _line_of(text, match.start()),
                    "Erlang/Elixir certifi has no fixed release yet",
                    f"{advisory.PENDING['erlang-certifi']} Point clients at the operating "
                    "system store or an updated bundle until it ships.",
                    _evidence(match.group(0)),
                )
            )

    # -- Node and Go ------------------------------------------------------------

    def _runtime(
        self,
        kind: str,
        rel: str,
        number: int,
        line: str,
        version: str,
        replace: tuple[int, int] | None,
    ) -> None:
        """Report a pinned Node.js or Go version; replace = span of version in line."""
        verdict = versions.node_verdict(version) if kind == "node" else versions.go_verdict(version)
        if verdict is None or verdict[0] == "fixed":
            return
        label = "Node.js" if kind == "node" else "Go"
        status, target = verdict
        if status == "unsupported":
            supported = (
                ", ".join(advisory.NODE_FIXED.values())
                if kind == "node"
                else ", ".join(advisory.GO_FIXED.values())
            )
            self.add(
                Finding(
                    f"{kind}-version",
                    "high",
                    rel,
                    number,
                    f"{label} {version} gets no fix",
                    f"Move to a maintained release with the fix ({supported} or later).",
                    _evidence(line),
                )
            )
            return
        edit = None
        if replace is not None and target is not None:
            start, end = replace
            edit = Edit(rel, number, number, (line[:start] + target + line[end:],))
        rebuild = " Rebuild and redeploy every binary built with it." if kind == "go" else ""
        self.add(
            Finding(
                f"{kind}-version",
                "high",
                rel,
                number,
                f"{label} {version} still trusts the compromised CAs",
                f"Upgrade to {label} {target} or later.{rebuild}",
                _evidence(line),
                edit,
            )
        )

    def version_file(self, rel: str, text: str, kind: str) -> None:
        for number, line in enumerate(text.split("\n"), 1):
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            offset = line.index(value) + (1 if value.startswith("v") else 0)
            raw = value.lstrip("v")
            self._runtime(kind, rel, number, line, raw, (offset, offset + len(raw)))
            return

    def tool_versions(self, rel: str, text: str) -> None:
        for number, line in enumerate(text.split("\n"), 1):
            match = _TOOL_VERSIONS.match(line)
            if not match:
                continue
            tool, version = match.group(2), match.group(3)
            if tool == "java" and self.java_evidence is None:
                self.java_evidence = (rel, number, _evidence(line))
            elif tool == "bun" and self.bun_evidence is None:
                self.bun_evidence = (rel, number, _evidence(line))
            elif tool in {"nodejs", "golang"}:
                kind = "node" if tool == "nodejs" else "go"
                self._runtime(kind, rel, number, line, version, (match.end(1), match.end(3)))

    def package_json(self, rel: str, text: str) -> None:
        if '"packageManager"' in text and '"bun@' in text and self.bun_evidence is None:
            self.bun_evidence = (rel, None, '"packageManager": "bun@..."')
        for number, line in enumerate(text.split("\n"), 1):
            match = _PACKAGE_NODE.match(line)
            if match:
                self._runtime("node", rel, number, line, match.group(2), match.span(2))

    def go_mod(self, rel: str, text: str) -> None:
        lines = text.split("\n")
        directive: tuple[int, str] | None = None
        toolchain: tuple[int, re.Match[str]] | None = None
        for number, line in enumerate(lines, 1):
            if match := _GO_DIRECTIVE.match(line):
                directive = (number, match.group(1))
            elif match := _GO_TOOLCHAIN.match(line):
                toolchain = (number, match)
        if toolchain is not None:
            number, match = toolchain
            self._runtime("go", rel, number, lines[number - 1], match.group(2), match.span(2))
        elif directive is not None:
            number, version = directive
            verdict = versions.go_verdict(version)
            if verdict is not None and verdict[0] == "vulnerable" and verdict[1]:
                self.add(
                    Finding(
                        "go-version",
                        "medium",
                        rel,
                        number,
                        f"go {version} lets GOTOOLCHAIN=auto select an affected toolchain",
                        f"Require Go {verdict[1]} or later (adds a toolchain line), then "
                        "rebuild and redeploy every binary.",
                        _evidence(lines[number - 1]),
                        Edit(rel, number + 1, number, (f"toolchain go{verdict[1]}",)),
                    )
                )
            elif verdict is not None and verdict[0] == "unsupported":
                self._runtime("go", rel, number, lines[number - 1], version, None)
        self.add(
            Finding(
                "go-rebuild",
                "medium",
                rel,
                directive[0] if directive else None,
                "Go binaries built before the fix still trust the compromised CAs",
                "Go copies crypto/x509 into every binary. Rebuild every service, CLI and "
                "sidecar from this module with Go 1.26.10 / 1.27.3 or later and redeploy.",
                _evidence(lines[directive[0] - 1]) if directive else "",
            )
        )

    # -- containers and CI --------------------------------------------------------

    def _image(self, rel: str, number: int, line: str, ref_start: int, ref: str) -> None:
        image, _, digest = ref.partition("@")
        if ":" in image.split("/")[-1]:
            repository, _, tag = image.rpartition(":")
        else:
            repository, tag = image, ""
        name = repository.split("/")[-1]
        if name == "bun":
            if self.bun_evidence is None:
                self.bun_evidence = (rel, number, _evidence(line))
            return
        if name in JAVA_IMAGES:
            self.add(
                Finding(
                    "java-cacerts",
                    "medium",
                    rel,
                    number,
                    f"{name} image trusts emSign ECC Root CA - G3",
                    f"{advisory.PENDING['java']} {JAVA_ACTION}",
                    _evidence(line),
                )
            )
            return
        if name not in {"node", "golang"}:
            return
        kind = "node" if name == "node" else "go"
        match = re.match(r"^(\d+\.\d+(?:\.\d+)?)", tag)
        if match is None or match.group(1).count(".") != 2:
            if match is not None and kind == "go":
                verdict = versions.go_verdict(match.group(1))
                if verdict is not None and verdict[0] == "unsupported":
                    self._runtime(kind, rel, number, line, match.group(1), None)
                    return
            self.add(
                Finding(
                    f"{kind}-image-floating",
                    "info",
                    rel,
                    number,
                    f"Floating {name} tag: rebuild to pick up the fix",
                    f"Rebuild with --pull once fixed {name} images are published, then redeploy.",
                    _evidence(line),
                )
            )
            return
        span = None
        if not digest:
            offset = ref_start + len(repository) + 1
            span = (offset, offset + len(match.group(1)))
        self._runtime(kind, rel, number, line, match.group(1), span)

    def dockerfile(self, rel: str, text: str) -> None:
        pinned = False
        first: tuple[int, str] | None = None
        for number, line in enumerate(text.split("\n"), 1):
            match = _FROM.match(line)
            if not match or match.group(1).lower() == "scratch" or "$" in match.group(1):
                continue
            first = first or (number, line)
            self._image(rel, number, line, match.start(1), match.group(1))
            if "@sha256:" in match.group(1):
                pinned = True
                self.add(
                    Finding(
                        "base-image-digest",
                        "medium",
                        rel,
                        number,
                        "Base image pinned by digest keeps its old ca-certificates",
                        "Move the digest to an image published after 2026-10-20, or upgrade "
                        "ca-certificates in a RUN step, then rebuild.",
                        _evidence(line),
                    )
                )
        if first is not None and not pinned:
            self.add(
                Finding(
                    "base-image-rebuild",
                    "info",
                    rel,
                    first[0],
                    "Images built before the fix ship the old ca-certificates",
                    "Rebuild with --pull so the distribution's updated ca-certificates is "
                    "included (or upgrade it in a RUN step), then redeploy.",
                    _evidence(first[1]),
                )
            )

    def ci(self, rel: str, text: str) -> None:
        for number, line in enumerate(text.split("\n"), 1):
            if match := _CI_VERSION.match(line):
                kind = "node" if match.group(2) == "node-version" else "go"
                value = match.group(3)
                if value.count(".") == 2:
                    self._runtime(kind, rel, number, line, value, match.span(3))
            elif match := _CI_IMAGE.match(line):
                self._image(rel, number, line, match.start(2), match.group(2))
            elif _CI_JAVA.match(line) and self.java_evidence is None:
                self.java_evidence = (rel, number, _evidence(line))

    # -- source patterns ----------------------------------------------------------

    def source(self, rel: str, suffix: str, text: str) -> None:
        language = _SUFFIX_RE.get(suffix)
        if not (
            _ANY_RE.search(text)
            or (language is not None and language.search(text))
            or _CUSTOM_CA.search(text)
        ):
            return
        custom: set[str] = set()
        for number, line in enumerate(text.split("\n"), 1):
            stripped = line.lstrip()
            if stripped.startswith(("//", "#")) and not stripped.startswith("#!"):
                continue
            hit = _ANY_RE.search(line) or (language.search(line) if language else None)
            if hit:
                self.add(
                    Finding(
                        "verification-disabled",
                        "high",
                        rel,
                        number,
                        "TLS certificate verification is switched off",
                        "Remove the bypass. With these keys public, anyone on the network path "
                        "can impersonate the server. Fix a failing chain by updating the trust "
                        "store, not by disabling verification.",
                        _evidence(line),
                    )
                )
            names = [m.group(1) for m in _CUSTOM_CA.finditer(line) if m.group(1) not in custom]
            if names:
                custom.update(names)
                self.add(
                    Finding(
                        "custom-trust-store",
                        "info",
                        rel,
                        number,
                        f"{', '.join(dict.fromkeys(names))} can point clients at a custom "
                        "trust store",
                        "Make sure any bundle or keystore it names has dropped emSign ECC "
                        "Root CA - G3 and the compromised intermediates.",
                        _evidence(line),
                    )
                )

    def pins(self, rel: str, text: str) -> None:
        lines = text.split("\n")
        seen: set[int] = set()
        for match in _DIGEST_TOKEN.finditer(text):
            token = match.group(0)
            label = _NEEDLES.get(token) or _NEEDLES.get(token.replace(":", "").lower())
            number = _line_of(text, match.start())
            if label is None or number in seen:
                continue
            seen.add(number)
            self.add(
                Finding(
                    "compromised-pin",
                    "critical",
                    rel,
                    number,
                    f"Reference to the {label}",
                    "Remove the pin or fingerprint and pin to a certificate from another CA.",
                    _evidence(lines[number - 1]),
                )
            )


def scan(root: Path) -> Report:
    return _Scan(root).run()

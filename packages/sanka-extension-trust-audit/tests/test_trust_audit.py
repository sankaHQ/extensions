# SPDX-License-Identifier: Apache-2.0
"""Detection rules, the review-bound patch directory and the subprocess transport."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from sanka_extension_trust_audit import EXTENSION_ID, VERSION, advisory, versions
from sanka_extension_trust_audit.adapter import handle
from sanka_extension_trust_audit.certs import read_binary, read_pem
from sanka_extension_trust_audit.scanner import scan
from sanka_extensions.code import ExtensionRequest, decode_response, encode_request

FIXTURES = Path(__file__).parent / "fixtures"
ROOT_G3 = (FIXTURES / "emsign-ecc-root-ca-g3.pem").read_text()
ISRG = (FIXTURES / "isrg-root-x1.pem").read_text()
INTERMEDIATE = (FIXTURES / "em-dv-tls-ca-g3c.pem").read_text()
CROSS = (FIXTURES / "em-dv-tls-ca-g3c-sectigo-cross.pem").read_text()
C1 = (FIXTURES / "emsign-root-ca-c1.pem").read_text()

PROJECT = {
    "requirements.txt": "requests==2.32.3\ncertifi==2024.2.2  # pinned\n",
    "uv.lock": 'version = 1\n\n[[package]]\nname = "certifi"\nversion = "2025.8.3"\n',
    "Cargo.lock": '[[package]]\nname = "webpki-roots"\nversion = "1.0.4"\n',
    "composer.lock": json.dumps(
        {"packages": [{"name": "composer/ca-bundle", "version": "1.5.1"}]}, indent=4
    ),
    "mix.lock": '%{\n  "certifi": {:hex, :certifi, "2.15.0", "abc", [:rebar3], [], "hexpm"},\n}\n',
    ".nvmrc": "v22.20.0\n",
    "go.mod": "module example.com/app\n\ngo 1.26.3\n\nrequire example.com/dep v1.0.0\n",
    "Dockerfile": (
        "FROM golang:1.26.4-alpine AS build\n"
        "RUN go build ./...\n"
        "FROM node:20.11.0-slim\n"
        "FROM eclipse-temurin:21-jre\n"
        "RUN curl -fsSLk https://example.com/install.sh | sh\n"
    ),
    ".github/workflows/ci.yml": (
        "jobs:\n  test:\n    steps:\n"
        "      - uses: actions/setup-go@v5\n        with:\n          go-version: '1.27.1'\n"
        "      - uses: actions/setup-node@v4\n        with:\n          node-version: 24.10.0\n"
    ),
    "pom.xml": "<project></project>\n",
    "app/client.py": "import requests\n\nrequests.get(URL, verify=False)\n# verify=False\n",
    "tests/test_client.py": "requests.get(URL, verify=False)\n",
    "main.go": "cfg := &tls.Config{InsecureSkipVerify: true}\n",
    "pins.json": json.dumps({"pins": ["sha256/Sd01PcK5GaN3BkURfQ5gwWEV59G8JZ7v5zSwDqQ0/l8="]}),
    "deploy/env.sh": "export NODE_EXTRA_CA_CERTS=/etc/ssl/custom.pem\n",
    "certs/bundle.pem": ISRG + "\n" + ROOT_G3,
    "certs/cross.pem": CROSS,
}


def write(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    write(root, PROJECT)
    return root


def request(
    root: Path, command: str, configuration: dict | None = None, reviewed: str | None = None
) -> ExtensionRequest:
    return ExtensionRequest(
        "case",
        command,
        str(root),
        str(root / ".sanka" / "extensions" / "sanka" / "trust-audit"),
        EXTENSION_ID,
        VERSION,
        "a" * 64,
        {},
        configuration or {},
        (),
        reviewed,
    )


def snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".sanka" not in path.relative_to(root).parts
    }


def test_inventory_covers_each_trust_decision(project: Path) -> None:
    report = scan(project)
    found = Counter((f.rule, f.severity, f.path) for f in report.findings)
    expected = {
        ("certifi-version", "high", "requirements.txt"),
        ("certifi-version", "high", "uv.lock"),
        ("webpki-roots-version", "high", "Cargo.lock"),
        ("composer-ca-bundle-version", "high", "composer.lock"),
        ("erlang-certifi-pending", "medium", "mix.lock"),
        ("node-version", "high", ".nvmrc"),
        ("go-version", "medium", "go.mod"),
        ("go-rebuild", "medium", "go.mod"),
        ("go-version", "high", "Dockerfile"),
        ("node-version", "high", "Dockerfile"),
        ("java-cacerts", "medium", "Dockerfile"),
        ("java-cacerts", "medium", "pom.xml"),
        ("verification-disabled", "high", "Dockerfile"),
        ("base-image-rebuild", "info", "Dockerfile"),
        ("go-version", "high", ".github/workflows/ci.yml"),
        ("node-version", "high", ".github/workflows/ci.yml"),
        ("verification-disabled", "high", "app/client.py"),
        ("verification-disabled", "medium", "tests/test_client.py"),
        ("verification-disabled", "high", "main.go"),
        ("compromised-pin", "critical", "pins.json"),
        ("custom-trust-store", "info", "deploy/env.sh"),
        ("removed-root-in-bundle", "high", "certs/bundle.pem"),
        ("compromised-ca-certificate", "critical", "certs/cross.pem"),
    }
    assert set(found) == expected
    # Commented-out bypasses are not findings.
    assert found[("verification-disabled", "high", "app/client.py")] == 1
    assert [f.severity for f in report.findings][:2] == ["critical", "critical"]


def test_unrelated_project_scans_clean(tmp_path: Path) -> None:
    write(tmp_path, {"README.md": "curl -k https://example.com\n", "src/app.rb": "puts 1\n"})
    response = handle(request(tmp_path, "scan"))
    assert response.outcome == "success"
    assert response.data["findings_total"] == 0


def test_apply_writes_patches_and_never_touches_project_files(project: Path) -> None:
    before = snapshot(project)
    findings = [f.as_json() for f in scan(project).findings]
    planned = handle(request(project, "plan"))
    assert planned.outcome == "success"
    plan_hash = planned.data["plan_hash"]
    assert planned.data["patch_files"] == [
        ".github/workflows/ci.yml",
        ".nvmrc",
        "Dockerfile",
        "certs/bundle.pem",
        "go.mod",
        "requirements.txt",
    ]
    applied = handle(
        request(project, "apply", {"extension_plan_hash": plan_hash}, reviewed="sha256:core")
    )
    assert applied.outcome == "success", applied.error
    output = project / "sanka-trust-patches"
    after = {k: v for k, v in snapshot(project).items() if not k.startswith("sanka-trust-patches/")}
    assert after == before
    manifest = json.loads((output / "MANIFEST.json").read_text())
    assert manifest["plan_hash"] == plan_hash
    assert (output / "files/requirements.txt").read_text() == (
        "requests==2.32.3\ncertifi==2026.10.19  # pinned\n"
    )
    assert (output / "files/.nvmrc").read_text() == "v22.23.4\n"
    assert "toolchain go1.26.10\n" in (output / "files/go.mod").read_text()
    dockerfile = (output / "files/Dockerfile").read_text()
    assert "FROM golang:1.26.10-alpine AS build" in dockerfile
    assert "FROM node:20.11.0-slim" in dockerfile  # unsupported line: manual
    assert "emSign ECC Root CA - G3" not in (output / "files/certs/bundle.pem").read_text()
    assert "ISRG Root X1" in (output / "files/certs/bundle.pem").read_text()

    # The combined patch reproduces the full copies.
    if shutil.which("git"):
        work = project.parent / "work"
        shutil.copytree(project, work, ignore=shutil.ignore_patterns(".sanka"))
        subprocess.run(
            ["git", "apply", "sanka-trust-patches/trust-audit.patch"], cwd=work, check=True
        )
        for copy in (output / "files").rglob("*"):
            if copy.is_file():
                rel = copy.relative_to(output / "files")
                assert (work / rel).read_bytes() == copy.read_bytes()

    # A later scan ignores the generated directory.
    assert [f.as_json() for f in scan(project).findings] == findings
    rescan = handle(request(project, "plan"))
    assert rescan.data["plan_hash"] == plan_hash


def test_apply_refuses_stale_plan_and_foreign_output(project: Path) -> None:
    plan_hash = handle(request(project, "plan")).data["plan_hash"]
    (project / ".nvmrc").write_text("v22.21.0\n")
    stale = handle(
        request(project, "apply", {"extension_plan_hash": plan_hash}, reviewed="sha256:core")
    )
    assert stale.outcome == "error"
    assert stale.error is not None and stale.error.code == "TRUST_AUDIT_PLAN_STALE"

    plan_hash = handle(request(project, "plan")).data["plan_hash"]
    (project / "mine").mkdir()
    (project / "mine" / "notes.txt").write_text("keep")
    with pytest.raises(FileExistsError):
        handle(
            request(
                project,
                "apply",
                {"extension_plan_hash": plan_hash, "output": "mine", "force": True},
                reviewed="sha256:core",
            )
        )
    assert (project / "mine" / "notes.txt").read_text() == "keep"
    # Outside the project, the project root itself, or names the printed shell
    # commands could not use verbatim.
    for output in ("../outside", ".", "./", "trust patches", "x;touch y"):
        with pytest.raises(ValueError):
            handle(
                request(
                    project,
                    "apply",
                    {"extension_plan_hash": plan_hash, "output": output},
                    reviewed="sha256:core",
                )
            )


def test_edits_keep_line_endings(tmp_path: Path) -> None:
    write(tmp_path, {"requirements.txt": "flask==3.0.0\r\ncertifi==2023.7.22"})
    plan_hash = handle(request(tmp_path, "plan")).data["plan_hash"]
    handle(request(tmp_path, "apply", {"extension_plan_hash": plan_hash}, reviewed="sha256:x"))
    patched = (tmp_path / "sanka-trust-patches/files/requirements.txt").read_bytes()
    assert patched == b"flask==3.0.0\r\ncertifi==2026.10.19"


def test_fingerprint_forms(tmp_path: Path) -> None:
    sha = "0164D7CEAE5BB909C272AF151FF44B1177AE9B47D9424B94F1D9558C30934D19"
    colons = ":".join(sha[i : i + 2] for i in range(0, 64, 2))
    spki_hex = "49dd353dc2b919a3770645117d0e60c16115e7d1bc259eefe734b00ea434fe5f"
    unrelated = "a" * 64
    write(tmp_path, {"pins.yaml": f"a: {sha}\nb: {colons}\nc: {spki_hex}\nd: {unrelated}\n"})
    assert [(f.rule, f.line) for f in scan(tmp_path).findings] == [
        ("compromised-pin", 1),
        ("compromised-pin", 2),
        ("compromised-pin", 3),
    ]


def test_nested_repositories_are_separate_projects(tmp_path: Path) -> None:
    write(tmp_path, {"worktree/.git": "gitdir: elsewhere\n", "worktree/.nvmrc": "22.1.0\n"})
    assert scan(tmp_path).findings == []


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        # Upper bounds that make the fixed certifi release unreachable.
        # certifi's advisory names 2026.10.19 as the first fixed release.
        (
            {"requirements.txt": "certifi==2026.10.18\n"},
            [("certifi-version", "requirements.txt", 1)],
        ),
        ({"requirements.txt": "certifi~=2025.8.3\n"}, [("certifi-version", "requirements.txt", 1)]),
        (
            {"requirements.txt": "certifi[extra] ==2025.*\n"},
            [("certifi-version", "requirements.txt", 1)],
        ),
        (
            {"requirements.txt": "certifi>=2024,<2026.10\n"},
            [("certifi-version", "requirements.txt", 1)],
        ),
        ({"requirements.txt": "certifi~=2026.10\ncertifi>=2024\n"}, []),
        ({"pyproject.toml": 'certifi = "^2025.8"\n'}, [("certifi-version", "pyproject.toml", 1)]),
        (
            {"pyproject.toml": 'certifi = { version = "~2025.8" }\n'},
            [("certifi-version", "pyproject.toml", 1)],
        ),
        (
            {"pyproject.toml": 'dependencies = ["certifi~=2025.8"]\n'},
            [("certifi-version", "pyproject.toml", 1)],
        ),
        ({"pyproject.toml": 'dependencies = ["certifi>=2023"]\n'}, []),
        # An older webpki-roots resolved alongside the fixed one is still linked.
        (
            {
                "Cargo.lock": '[[package]]\nname = "webpki-roots"\nversion = "0.25.4"\n\n'
                '[[package]]\nname = "webpki-roots"\nversion = "1.0.10"\n'
            },
            [("webpki-roots-version", "Cargo.lock", 3)],
        ),
        # A 0.26 compatibility release that re-exports the fixed 1.x crate is not.
        (
            {
                "Cargo.lock": '[[package]]\nname = "webpki-roots"\nversion = "0.26.11"\n'
                'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
                'dependencies = [\n "webpki-roots 1.0.10",\n]\n\n'
                '[[package]]\nname = "webpki-roots"\nversion = "1.0.10"\n'
            },
            [],
        ),
        # JDK images: OpenJDK blocks the certificates after 21.0.13 (CVE-2026-91403).
        (
            {"Dockerfile": "FROM eclipse-temurin:21.0.12_7-jre\n"},
            [("java-cacerts", "Dockerfile", 1), ("base-image-rebuild", "Dockerfile", 1)],
        ),
        (
            {"Dockerfile": "FROM eclipse-temurin:21.0.14_7-jre\n"},
            [("base-image-rebuild", "Dockerfile", 1)],
        ),
        ({"Dockerfile": "FROM amazoncorretto:8u520\n"}, [("base-image-rebuild", "Dockerfile", 1)]),
        # Roots NSS removed at eMudhra's request are reported, never cut out.
        (
            {"certs/bundle.pem": ISRG + "\n" + C1},
            [("nss-removed-root-in-bundle", "certs/bundle.pem", 47)],
        ),
    ],
)
def test_release_boundaries(
    tmp_path: Path, files: dict[str, str], expected: list[tuple[str, str, int]]
) -> None:
    write(tmp_path, files)
    found = [(f.rule, f.path, f.line) for f in scan(tmp_path).findings]
    assert sorted(found) == sorted(expected)


def test_hashed_requirements_are_reported_not_patched(tmp_path: Path) -> None:
    write(tmp_path, {"requirements.txt": "certifi==2024.2.2 \\\n    --hash=sha256:abc\n"})
    (finding,) = scan(tmp_path).findings
    assert finding.rule == "certifi-version" and finding.edit is None


def test_certificate_readers_match_published_identities() -> None:
    (intermediate,) = read_pem(INTERMEDIATE.encode())
    assert intermediate.sha256.upper() == (
        "0164D7CEAE5BB909C272AF151FF44B1177AE9B47D9424B94F1D9558C30934D19"
    )
    assert intermediate.spki_sha256_b64 in advisory.COMPROMISED_SPKI
    assert intermediate.subject_cn == "EM DV TLS CA - G3C"
    assert intermediate.issuer_cn == "emSign Root TLS CA - G3"
    (root,) = read_pem(ROOT_G3.encode())
    assert root.self_issued and root.subject_cn == "emSign ECC Root CA - G3"
    der = base64.b64decode("".join(INTERMEDIATE.strip().splitlines()[1:-1]))
    (embedded,) = read_binary(b"\xfe\xed\xfe\xed\x30\x82junk" + der + b"tail")
    assert embedded.sha256 == intermediate.sha256


def test_version_rules() -> None:
    assert versions.node_verdict("22.20.0") == ("vulnerable", "22.23.4")
    assert versions.node_verdict("22.23.4") == ("fixed", None)
    assert versions.node_verdict("20.11.0") == ("unsupported", None)
    assert versions.node_verdict("28.0.0") == ("fixed", None)
    assert versions.go_verdict("1.27.2") == ("vulnerable", "1.27.3")
    assert versions.go_verdict("go1.26.10") == ("fixed", None)
    assert versions.go_verdict("1.25.9") == ("unsupported", None)
    assert versions.go_verdict("1.28") == ("fixed", None)
    assert not versions.composer_ca_bundle_safe("1.5.4")
    assert versions.composer_ca_bundle_safe("1.4.16")
    assert versions.composer_ca_bundle_safe("1.6.0")


def test_subprocess_protocol(project: Path) -> None:
    payload = json.dumps(encode_request(request(project, "scan")))
    result = subprocess.run(
        [sys.executable, "-m", "sanka_extension_trust_audit"],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    response = decode_response(json.loads(result.stdout))
    assert response.outcome == "success"
    malformed = subprocess.run(
        [sys.executable, "-m", "sanka_extension_trust_audit"],
        input="{}",
        capture_output=True,
        text=True,
        check=False,
    )
    assert malformed.returncode == 2 and malformed.stdout == ""

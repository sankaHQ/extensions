# SPDX-License-Identifier: Apache-2.0
"""The executable lifecycle owns capture, review binding and observed parity."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sanka_extensions.code import ExtensionRequest, encode_request

FIXTURE = Path(__file__).parent / "fixtures/greetings"
CONTROLLER = "src/main/java/example/greetings/GreetingController.java"
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(os.getenv("SANKA_JAVA_TESTS") != "1", reason="opt-in Java toolchain lane"),
]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    shutil.copytree(FIXTURE, root)
    return root.resolve()


def call(
    root: Path,
    command: str,
    *,
    plan: str | None = None,
    reviewed: bool = True,
    artifact: Path | None = None,
) -> dict:
    config = {"target": "python-fastapi"}
    if plan is not None:
        config["extension_plan_hash"] = plan
    request = ExtensionRequest(
        request_id="java-contract",
        command=command,
        project_root=str(root),
        artifact_root=str(artifact or root / ".sanka/java"),
        extension_id="sanka/java-to-python",
        extension_version="0.1.0a2",
        manifest_digest="0" * 64,
        fingerprint={},
        configuration=config,
        prior_artifacts=(),
        reviewed_plan_hash="runtime-reviewed" if reviewed else None,
    )
    result = subprocess.run(
        [sys.executable, "-m", "sanka_extension_java_to_python"],
        input=json.dumps(encode_request(request)),
        text=True,
        capture_output=True,
        timeout=240,
        check=False,
    )
    response = json.loads(result.stdout)
    assert result.returncode == (0 if response["outcome"] == "success" else 1), result.stderr
    return response


def planned(root: Path) -> str:
    result = call(root, "plan")
    assert result["outcome"] == "success", result
    assert result["data"]["blockers"] == [], result
    return result["data"]["plan_hash"]


def test_full_lifecycle_and_real_mismatch(project: Path) -> None:
    assert call(project, "scan")["data"]["ready"]
    plan = planned(project)
    assert planned(project) == plan
    assert call(project, "apply", plan=plan)["outcome"] == "success"
    assert call(project, "test")["outcome"] == "success"
    result = call(project, "verify")
    assert result["outcome"] == "success", result
    report = result["data"]
    assert len(report["steps"]) == 9
    bodies = {row["id"]: row["body"] for row in report["candidate_observations"]}
    assert bodies["unicode"]["message"] == "Hello, 世界"
    assert bodies["repeated"]["message"] == "Hello, Alice,Bob"
    assert bodies["repeated-empty"]["message"] == "Hello, ,"
    assert bodies["status"]["large"] == 9007199254740993
    assert bodies["default"]["note"] is None
    generated = project / ".sanka/java/python-fastapi/app.py"
    generated.write_text(generated.read_text().replace("Hello, ", "Goodbye, "))
    mismatch = call(project, "verify")
    assert mismatch["outcome"] == "error"
    changed = json.loads((project / ".sanka/java/verify.json").read_text())
    assert changed["ok"] is False
    assert changed["candidate_digest"] != report["candidate_digest"]
    for source in FIXTURE.rglob("*"):
        if source.is_file():
            assert (project / source.relative_to(FIXTURE)).read_bytes() == source.read_bytes()


@pytest.fixture
def crud_project(tmp_path: Path) -> Path:
    root = tmp_path / "orders"
    shutil.copytree(FIXTURE.parent / "orders", root)
    return root.resolve()


@pytest.mark.skipif(
    not os.getenv("SANKA_JAVA_POSTGRES_CONFIG"), reason="opt-in private PostgreSQL fixture"
)
def test_crud_database_state_and_rollback(crud_project: Path) -> None:
    scenarios_file = crud_project / "sanka-verify.json"
    scenarios = json.loads(scenarios_file.read_text())
    created = next(i for i, row in enumerate(scenarios["scenarios"]) if row["id"] == "create-exact")
    scenarios["scenarios"][created + 1 : created + 1] = [
        {"id": name, "method": "GET", "path": "/orders/" + value, "expected_status": status}
        for name, value, status in (
            ("arabic-id", "\u0661", 200),
            ("fullwidth-id", "\uff11", 200),
            ("mixed-id", "\u0660\u06f1", 200),
            ("signed-unicode-id", "+\u0661", 200),
            ("unicode-hex-id", "0x\uff11", 200),
            ("fullwidth-hex-id", "0x\uff26", 404),
            ("astral-id", "\U0001d7d9", 400),
            ("superscript-id", "\xb9", 400),
            (
                "unicode-overflow-id",
                "\u06f9\u06f2\u06f2\u06f3\u06f3\u06f7\u06f2\u06f0\u06f3\u06f6\u06f8\u06f5\u06f4\u06f7\u06f7\u06f5\u06f8\u06f0\u06f8",
                400,
            ),
        )
    ]
    scenarios_file.write_text(json.dumps(scenarios, ensure_ascii=False))
    source = {
        p.relative_to(crud_project): p.read_bytes() for p in crud_project.rglob("*") if p.is_file()
    }
    scanned = call(crud_project, "scan")
    assert scanned["data"]["ready"], scanned
    plan = planned(crud_project)
    assert call(crud_project, "apply", plan=plan)["outcome"] == "success"
    native = call(crud_project, "test")
    assert native["outcome"] == "success", native
    verified = call(crud_project, "verify")
    assert verified["outcome"] == "success", verified
    report = verified["data"]
    observations = {row["id"]: row for row in report["candidate_observations"]}
    for name in ("arabic-id", "fullwidth-id", "mixed-id", "signed-unicode-id", "unicode-hex-id"):
        assert observations[name]["body"]["id"] == 1
    assert observations["create-exact"]["body"]["amount"] == "999999999999.99"
    assert observations["create-scale"]["body"]["amount"] == "1.2"
    assert observations["read-scale"]["body"]["amount"] == "1.20"
    assert observations["create-exact"]["body"]["note"] is None
    assert observations["rollback"]["tables"] == observations["after-rollback"]["tables"]
    assert [r["id"] for r in observations["rollback"]["tables"]["orders"]] == [1, 2]
    assert observations["rollback"]["sequences"]["orders_id_seq"]["last_value"] == 3
    assert observations["create-after-rollback"]["body"]["id"] == 4
    assert observations["invalid-update"]["tables"] == observations["read-update"]["tables"]
    assert observations["delete"]["body"] is None
    assert observations["negative-zero"]["body"]["amount"] == "0.00"
    for name, content in source.items():
        assert (crud_project / name).read_bytes() == content
    candidate = crud_project / ".sanka/java/python-fastapi/app.py"
    candidate.write_text(
        candidate.read_text().replace(
            "payload = _dump(entity)", 'payload = _dump(entity)\n        entity.name += " changed"'
        )
    )
    rejected = call(crud_project, "verify")
    assert rejected["outcome"] == "error"
    mismatch = json.loads((crud_project / ".sanka/java/verify.json").read_text())
    failed_create = next(s for s in mismatch["steps"] if s["id"] == "create-exact")
    assert any("tables" in p for p in failed_create["problems"])
    assert mismatch["candidate_digest"] != report["candidate_digest"]


@pytest.mark.parametrize(
    "filename,old,new",
    [
        ("OrderController.java", "@Transactional", "@Transactional(readOnly=true)"),
        (
            "OrderController.java",
            "return repository.findAllByOrderByIdAsc();",
            "repository.deleteAll(); return repository.findAllByOrderByIdAsc();",
        ),
        ("Order.java", "JsonFormat.Shape.STRING", "JsonFormat.Shape.NUMBER"),
        ("Order.java", "public Long id;", "public Long id; static { System.exit(1); }"),
        ("OrderInput.java", "@Size(max=80)", "@Size(max=79)"),
        ("OrderRepository.java", "findAllByOrderByIdAsc", "findAllByOrderByNameAsc"),
        ("Errors.java", "HttpStatus.NOT_FOUND", "HttpStatus.OK"),
    ],
)
def test_crud_unknown_behavior_blocks_whole_dependency_closure(
    crud_project: Path, filename: str, old: str, new: str
) -> None:
    path = crud_project / "src/main/java/example/orders" / filename
    path.write_text(path.read_text().replace(old, new))
    scanned = call(crud_project, "scan")
    assert scanned["outcome"] == "success"
    assert not scanned["data"]["ready"]
    result = call(crud_project, "plan")
    assert result["data"]["blockers"]
    assert result["data"]["files"] == {}


@pytest.mark.parametrize(
    "change",
    [
        "missing_review",
        "wrong_hash",
        "source",
        "scenarios",
        "collision",
        "artifact_symlink",
        "source_symlink",
        "stale_report",
    ],
)
def test_review_and_filesystem_guards(project: Path, tmp_path: Path, change: str) -> None:
    plan = planned(project)
    artifact = None
    command = "apply"
    if change == "source":
        p = project / CONTROLLER
        p.write_text(p.read_text().replace("Hello, ", "Goodbye, "))
    elif change == "scenarios":
        p = project / "sanka-verify.json"
        p.write_text(p.read_text().replace("Alice", "Carol"))
    elif change == "collision":
        output = project / ".sanka/java/python-fastapi"
        output.mkdir()
        (output / "keep.txt").write_text("existing user work")
    elif change == "artifact_symlink":
        (project / ".sanka/elsewhere").symlink_to(tmp_path, target_is_directory=True)
        artifact = project / ".sanka/elsewhere/escape"
    elif change == "source_symlink":
        (project / "src/main/java/alias").symlink_to(tmp_path, target_is_directory=True)
    elif change == "stale_report":
        command = "verify"
        (project / ".sanka/java/verify.json").write_text('{"ok":true}')
        (project / "pom.xml").write_text("invalid POM")
    response = call(
        project,
        command,
        plan="wrong" if change == "wrong_hash" else plan,
        reviewed=change != "missing_review",
        artifact=artifact,
    )
    assert response["outcome"] == "error"
    if change == "collision":
        assert (project / ".sanka/java/python-fastapi/keep.txt").read_text() == "existing user work"
    elif change == "stale_report":
        assert not (project / ".sanka/java/verify.json").exists()
    else:
        assert not (project / ".sanka/java/python-fastapi").exists()


@pytest.mark.parametrize(
    ("file", "old", "new"),
    [
        ("pom.xml", "spring-boot-starter-web", "spring-boot-starter-security"),
        ("pom.xml", "spring-boot-starter-web", "spring-boot-starter-data-jpa"),
        ("pom.xml", "<properties>", "<profiles/><properties>"),
        (CONTROLLER, "import java.util.Map;", "import java.util.*;"),
        (
            CONTROLLER,
            "public class GreetingController {",
            'public class GreetingController { static { System.setProperty("sanka-probe","bad"); }',
        ),
        (CONTROLLER, '"Hello, " + name', "name.toUpperCase()"),
        (CONTROLLER, 'defaultValue="world"', 'defaultValue="${USER}"'),
        (CONTROLLER, '"service", "greetings"', '"service", "greetings", "service", "duplicate"'),
        (
            "src/main/java/example/greetings/Greeting.java",
            "public record Greeting",
            "@Deprecated public record Greeting",
        ),
    ],
)
def test_unsupported_source_blocks_generation(project: Path, file: str, old: str, new: str) -> None:
    path = project / file
    path.write_text(path.read_text().replace(old, new))
    scanned = call(project, "scan")
    assert scanned["outcome"] == "success", scanned
    assert scanned["data"]["ready"] is False
    plan = call(project, "plan")
    assert plan["data"]["blockers"]
    assert plan["data"]["files"] == {}
    assert call(project, "apply", plan=plan["data"]["plan_hash"])["outcome"] == "error"


@pytest.mark.parametrize(
    "payload",
    [[], [{"id": "wrong-route", "method": "GET", "path": "/missing", "expected_status": 200}]],
)
def test_missing_scenario_coverage_blocks_plan(project: Path, payload: list) -> None:
    (project / "sanka-verify.json").write_text(json.dumps(payload))
    plan = call(project, "plan")
    assert plan["data"]["blockers"]
    assert not plan["data"]["files"]


@pytest.mark.parametrize("name", ["bool", "str"])
def test_record_names_cannot_shadow_generated_types(project: Path, name: str) -> None:
    source = project / "src/main/java/example/greetings" / f"{name}.java"
    source.write_text(f"package example.greetings; public record {name}(String value) {{}}")
    scanned = call(project, "scan")
    assert scanned["data"]["ready"] is False
    plan = call(project, "plan")
    assert plan["data"]["blockers"]
    assert not plan["data"]["files"]

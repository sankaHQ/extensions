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
        extension_version="0.1.0a1",
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

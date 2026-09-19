# SPDX-License-Identifier: Apache-2.0
"""Integrity and destination contract coverage for the complete vertical slice."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from sanka_extension_llm_to_jev.common import canonical, object_digest, source_files
from sanka_extension_llm_to_jev.generator import render_candidate
from sanka_extension_llm_to_jev.lifecycle import handle
from sanka_extension_llm_to_jev.planning import build_plan
from sanka_extension_llm_to_jev.scanner import scan_project

from sanka_extensions.code import ExtensionRequest

FIXTURE = Path(__file__).parent / "fixtures" / "positive"


@pytest.fixture
def prepared(tmp_path: Path) -> tuple[ExtensionRequest, dict]:
    root = tmp_path.resolve() / "source"
    shutil.copytree(FIXTURE, root)
    (root / "requirements.txt").write_text("openai==3.16.2\n")
    artifacts = tmp_path.resolve() / "artifacts"
    artifacts.mkdir()
    inventory = scan_project(root)
    spec = json.loads((root / "jev-decision.json").read_text())
    plan = build_plan(root, spec, inventory, "a" * 64)
    (artifacts / "migration-plan.json").write_text(canonical(plan))
    request = ExtensionRequest(
        request_id="unit",
        command="apply",
        project_root=str(root),
        artifact_root=str(artifacts),
        extension_id="sanka/llm-to-jev",
        extension_version="0.1.0a1",
        manifest_digest="a" * 64,
        fingerprint={},
        configuration={"extension_plan_hash": plan["plan_hash"]},
        prior_artifacts=(str(artifacts / "migration-plan.json"),),
        reviewed_plan_hash="cli-reviewed",
    )
    return request, plan


def test_apply_preserves_source_and_is_deterministic(
    prepared: tuple[ExtensionRequest, dict],
) -> None:
    request, plan = prepared
    source = source_files(Path(request.project_root))
    first = handle(request)
    assert first.outcome == "success", first.error
    candidate = Path(request.artifact_root) / "candidate"
    assert source_files(Path(request.project_root)) == source
    assert source_files(candidate) == {**source, **plan["generated_digests"]}
    second = handle(request)
    assert second == first
    assert "OpenAI" not in (candidate / "classifier.py").read_text()
    assert (candidate / "requirements.txt").read_bytes() == (
        Path(request.project_root) / "requirements.txt"
    ).read_bytes()


@pytest.mark.parametrize("target", ["source", "decision", "plan", "candidate", "manifest"])
def test_rejects_tampering(prepared: tuple[ExtensionRequest, dict], target: str) -> None:
    request, _plan = prepared
    assert handle(request).outcome == "success"
    root, artifacts = Path(request.project_root), Path(request.artifact_root)
    if target == "source":
        (root / "classifier.py").write_text((root / "classifier.py").read_text() + "\n# changed\n")
    elif target == "decision":
        spec = json.loads((root / "jev-decision.json").read_text())
        spec["question"]["text"] = "Changed reviewed meaning"
        (root / "jev-decision.json").write_text(canonical(spec))
    elif target == "plan":
        plan = json.loads((artifacts / "migration-plan.json").read_text())
        plan["changed_files"]["injected.py"] = "raise RuntimeError()\n"
        (artifacts / "migration-plan.json").write_text(canonical(plan))
    elif target == "candidate":
        (artifacts / "candidate" / "classifier.py").write_text("tampered\n")
    else:
        request = replace(request, manifest_digest="b" * 64)
    result = handle(request)
    assert result.outcome == "error"


@pytest.mark.parametrize("target", ["source", "candidate", "artifact_root", "plan"])
def test_symlinks_rejected(
    prepared: tuple[ExtensionRequest, dict], target: str, tmp_path: Path
) -> None:
    request, _plan = prepared
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    artifacts = Path(request.artifact_root)
    if target == "source":
        (Path(request.project_root) / "escape").symlink_to(outside, target_is_directory=True)
    elif target == "candidate":
        (artifacts / "candidate").symlink_to(outside, target_is_directory=True)
    elif target == "artifact_root":
        link = tmp_path.resolve() / "linked"
        link.symlink_to(artifacts, target_is_directory=True)
        request = replace(request, artifact_root=str(link))
    else:
        plan = artifacts / "migration-plan.json"
        copied = outside / plan.name
        plan.rename(copied)
        plan.symlink_to(copied)
    assert handle(request).outcome == "error"


def test_overlap_and_unreviewed_plan_rejected(prepared: tuple[ExtensionRequest, dict]) -> None:
    request, _plan = prepared
    assert handle(replace(request, artifact_root=request.project_root)).outcome == "error"
    assert handle(replace(request, reviewed_plan_hash=None)).outcome == "error"


def test_manual_consumer_preserved(prepared: tuple[ExtensionRequest, dict]) -> None:
    request, _plan = prepared
    root = Path(request.project_root)
    source = root / "classifier.py"
    source.write_text(
        source.read_text()
        + "\n\ndef manual(text):\n    return client.responses.create(input=text)\n"
    )
    inventory = scan_project(root)
    spec = json.loads((root / "jev-decision.json").read_text())
    changed = render_candidate(root, spec, inventory)
    assert "client = OpenAI()" in changed["classifier.py"]
    assert "return client.responses.create(input=text)" in changed["classifier.py"]


def test_destination_tests_and_verify(prepared: tuple[ExtensionRequest, dict]) -> None:
    python = os.environ.get("JEV_TEST_PYTHON")
    if not python:
        pytest.skip("JEV_TEST_PYTHON names an isolated destination with typesafe-sdk==0.7.0")
    request, plan = prepared
    assert handle(request).outcome == "success"
    request = replace(request, configuration={**request.configuration, "candidate_python": python})
    tested = handle(replace(request, command="test"))
    assert tested.outcome == "success", tested.error
    assert tested.data["mode"] == "mock"
    assert tested.data["model_quality"] == "not_evaluated"
    verified = handle(replace(request, command="verify"))
    assert verified.outcome == "success", verified.error
    assert verified.data["plan_hash"] == plan["plan_hash"]
    assert verified.data["candidate_digest"] == object_digest(
        source_files(Path(request.artifact_root) / "candidate")
    )


def test_requires_destination_interpreter(prepared: tuple[ExtensionRequest, dict]) -> None:
    request, _plan = prepared
    assert handle(request).outcome == "success"
    result = handle(replace(request, command="test"))
    assert result.outcome == "error"
    assert "candidate_python" in result.error.message


def test_manual_sites_prevent_complete_verification(
    prepared: tuple[ExtensionRequest, dict],
) -> None:
    python = os.environ.get("JEV_TEST_PYTHON")
    if not python:
        pytest.skip("requires destination SDK environment")
    request, _plan = prepared
    root, artifacts = Path(request.project_root), Path(request.artifact_root)
    (root / "manual.py").write_text(
        "from openai import OpenAI\nclient = OpenAI()\n"
        "def explain(text):\n    return client.responses.create(input=text)\n"
    )
    inventory = scan_project(root)
    spec = json.loads((root / "jev-decision.json").read_text())
    plan = build_plan(root, spec, inventory, request.manifest_digest)
    (artifacts / "migration-plan.json").write_text(canonical(plan))
    request = replace(
        request,
        configuration={
            "extension_plan_hash": plan["plan_hash"],
            "candidate_python": python,
        },
    )
    assert handle(request).outcome == "success"
    assert (artifacts / "candidate" / "manual.py").read_bytes() == (root / "manual.py").read_bytes()
    assert handle(replace(request, command="test")).outcome == "success"
    assert handle(replace(request, command="verify")).outcome == "error"
    report = json.loads((artifacts / "verification-report.json").read_text())
    assert report["status"] == "manual_review_required"
    assert not report["passed"]
    assert not report["complete_migration"]


def test_changed_report_rejected(prepared: tuple[ExtensionRequest, dict]) -> None:
    python = os.environ.get("JEV_TEST_PYTHON")
    if not python:
        pytest.skip("requires destination SDK environment")
    request, _plan = prepared
    assert handle(request).outcome == "success"
    request = replace(request, configuration={**request.configuration, "candidate_python": python})
    assert handle(replace(request, command="test")).outcome == "success"
    path = Path(request.artifact_root) / "compatibility-report.json"
    report = json.loads(path.read_text())
    report["candidate_digest"] = "0" * 64
    path.write_text(canonical(report))
    assert handle(replace(request, command="verify")).outcome == "error"


def test_generated_path_collision_rejected(prepared: tuple[ExtensionRequest, dict]) -> None:
    request, _plan = prepared
    root = Path(request.project_root)
    (root / "requirements-jev.txt").write_text("do not overwrite\n")
    spec = json.loads((root / "jev-decision.json").read_text())
    with pytest.raises(ValueError, match="already exists"):
        render_candidate(root, spec, scan_project(root))


@pytest.mark.parametrize("statement", ["client = OpenAI()", "from openai import OpenAI"])
def test_shared_line_statements_preserved(
    prepared: tuple[ExtensionRequest, dict],
    statement: str,
) -> None:
    request, _plan = prepared
    root = Path(request.project_root)
    path = root / "classifier.py"
    path.write_text(path.read_text().replace(statement, statement + "; KEEP = 'preserve'"))
    spec = json.loads((root / "jev-decision.json").read_text())
    with pytest.raises(ValueError, match="shared-line"):
        render_candidate(root, spec, scan_project(root))
    assert "KEEP = 'preserve'" in path.read_text()

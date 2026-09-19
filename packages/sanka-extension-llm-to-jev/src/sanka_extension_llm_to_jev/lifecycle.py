# SPDX-License-Identifier: Apache-2.0
"""Reviewed-plan execution and destination-owned, explicitly mocked verification."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from sanka_extensions.code import (
    ExtensionRequest,
    ExtensionResponse,
    failure_response,
    success_response,
)

from .common import IGNORED, canonical, digest, object_digest, safe_path, source_files
from .decision import validate_spec
from .planning import build_plan
from .scanner import scan_project


def _real_directory(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink() or path.absolute() != path.resolve():
        raise ValueError("directories must be absolute and contain no symlinks")
    return path


def _context(request: ExtensionRequest) -> tuple[Path, Path, dict[str, Any], dict[str, Any]]:
    root = _real_directory(Path(request.project_root))
    artifacts = _real_directory(Path(request.artifact_root))
    if artifacts == root or root.is_relative_to(artifacts):
        raise ValueError("artifact directory must not contain the source")
    if artifacts.is_relative_to(root) and not any(
        part in IGNORED for part in artifacts.relative_to(root).parts
    ):
        raise ValueError(
            "artifacts inside source must be under an ignored directory such as .sanka"
        )
    paths = [
        Path(path) for path in request.prior_artifacts if Path(path).name == "migration-plan.json"
    ]
    if not paths:
        paths = [artifacts / "migration-plan.json"]
    if len(paths) != 1:
        raise ValueError("exactly one migration-plan.json is required")
    path = paths[0]
    if path != safe_path(artifacts, "migration-plan.json"):
        raise ValueError("plan must belong to the current artifact directory")
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError("plan path must not contain symlinks")
    plan = json.loads(path.read_text(encoding="utf-8"))
    supplied = request.configuration.get("extension_plan_hash")
    if not supplied or supplied != plan.get("plan_hash"):
        raise ValueError("extension plan hash does not match reviewed plan")
    expected_hash = object_digest({key: value for key, value in plan.items() if key != "plan_hash"})
    if plan.get("plan_hash") != expected_hash:
        raise ValueError("plan integrity mismatch")
    if not request.reviewed_plan_hash:
        raise ValueError("a reviewed CLI plan hash is required")
    spec_relative = request.configuration.get("decision_spec", "jev-decision.json")
    if not isinstance(spec_relative, str):
        raise ValueError("decision_spec must be a relative path")
    spec = json.loads(safe_path(root, spec_relative).read_text(encoding="utf-8"))
    inventory = scan_project(root)
    validate_spec(spec, inventory)
    rebuilt = build_plan(root, spec, inventory, request.manifest_digest)
    if rebuilt != plan:
        raise ValueError(
            "source, decision, extension, or generated plan changed; create a new plan"
        )
    artifacts.mkdir(parents=True, exist_ok=True)
    return root, artifacts, plan, inventory


def _expected(plan: dict[str, Any], inventory: dict[str, Any]) -> dict[str, str]:
    return {**inventory["files"], **plan["generated_digests"]}


def _candidate(artifacts: Path, plan: dict[str, Any], inventory: dict[str, Any]) -> Path:
    candidate = safe_path(artifacts, "candidate")
    if not candidate.is_dir() or source_files(candidate) != _expected(plan, inventory):
        raise ValueError("candidate integrity mismatch; apply the unchanged reviewed plan")
    return candidate


def _report(plan: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    remaining = [site for site in inventory["call_sites"] if site["id"] != plan["call_site"]["id"]]
    return {
        "schema_version": "sanka-jev-report/v1",
        "bindings": plan["bindings"],
        "plan_hash": plan["plan_hash"],
        "generated_digests": plan["generated_digests"],
        "candidate_digest": object_digest(_expected(plan, inventory)),
        "manual_call_sites": plan["manual_call_sites"],
        "unconverted_call_sites": remaining,
        "complete_migration": not remaining,
        "model_quality": "not_evaluated",
        "confidence_status": "uncalibrated",
        "live_provider_calls": 0,
    }


def _apply(root: Path, artifacts: Path, plan: dict[str, Any], inventory: dict[str, Any]) -> Path:
    candidate = safe_path(artifacts, "candidate")
    if candidate.exists():
        return _candidate(artifacts, plan, inventory)
    staging = Path(tempfile.mkdtemp(prefix=".candidate-", dir=artifacts))
    try:
        for relative, expected_digest in inventory["files"].items():
            source = safe_path(root, relative)
            contents = source.read_bytes()
            if digest(contents) != expected_digest:
                raise ValueError("source changed during apply")
            target = safe_path(staging, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(contents)
        for relative, contents in plan["changed_files"].items():
            target = safe_path(staging, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding="utf-8")
        if source_files(root) != inventory["files"]:
            raise ValueError("source changed during apply")
        if source_files(staging) != _expected(plan, inventory):
            raise ValueError("generated candidate integrity mismatch")
        staging.rename(candidate)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return candidate


def _test(request: ExtensionRequest, candidate: Path, plan: dict[str, Any]) -> dict[str, Any]:
    python = request.configuration.get("candidate_python")
    if not isinstance(python, str) or not Path(python).is_absolute() or not Path(python).is_file():
        raise ValueError("candidate_python must name an existing absolute destination interpreter")
    module = f"test_jev_{plan['decision']['decision_id']}"
    runner = (
        "import importlib.metadata,json,platform,sys,unittest;"
        "sys.path.insert(0,sys.argv[1]);"
        "print(json.dumps({'python':platform.python_version(),"
        "'typesafe_sdk':importlib.metadata.version('typesafe-sdk')}));"
        "result=unittest.TextTestRunner(verbosity=2).run("
        "unittest.defaultTestLoader.loadTestsFromName(sys.argv[2]));"
        "sys.exit(0 if result.wasSuccessful() else 1)"
    )
    result = subprocess.run(
        [python, "-B", "-I", "-c", runner, str(candidate), module],
        cwd=candidate,
        env={
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "LANG", "LC_ALL", "SYSTEMROOT"}
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    # The mock test process receives no provider credentials. Its sanitized log
    # contains test names/results, never provider responses or request text.
    versions = json.loads(result.stdout.splitlines()[0]) if result.stdout.strip() else {}
    return {
        "mode": "mock",
        "passed": result.returncode == 0 and versions.get("typesafe_sdk") == "0.7.0",
        "versions": versions,
        "candidate_python": python,
        "exit_code": result.returncode,
        "test_output": result.stderr,
        "provider_latency": None,
    }


def handle(request: ExtensionRequest) -> ExtensionResponse:
    try:
        root, artifacts, plan, inventory = _context(request)
        report = _report(plan, inventory)
        if request.command == "apply":
            candidate = _apply(root, artifacts, plan, inventory)
            return success_response(
                request,
                data={"candidate": str(candidate), "plan_hash": plan["plan_hash"]},
                artifacts=[str(candidate)],
                limitations=["Model quality and economics have not been evaluated."],
            )
        candidate = _candidate(artifacts, plan, inventory)
        if request.command == "test":
            report.update(_test(request, candidate, plan))
            _candidate(artifacts, plan, inventory)
            name = "compatibility-report.json"
        elif request.command == "verify":
            compatibility = safe_path(artifacts, "compatibility-report.json")
            tested = json.loads(compatibility.read_text(encoding="utf-8"))
            if any(tested.get(key) != value for key, value in report.items()):
                raise ValueError("compatibility report no longer binds this candidate")
            if tested.get("mode") != "mock" or tested.get("passed") is not True:
                raise ValueError("passing mocked destination compatibility tests are required")
            report.update(
                {
                    "mode": "static_and_mock",
                    "compatibility_report_digest": digest(compatibility.read_bytes()),
                    "passed": not report["unconverted_call_sites"],
                    "candidate_integrity": True,
                    "dependency_ownership": "destination",
                    "status": "complete"
                    if not report["unconverted_call_sites"]
                    else "manual_review_required",
                }
            )
            name = "verification-report.json"
        else:
            raise ValueError(f"unsupported lifecycle command: {request.command}")
        output = safe_path(artifacts, name)
        output.write_text(canonical(report), encoding="utf-8")
        if not report["passed"]:
            return failure_response(
                request,
                code="verification_incomplete",
                message=f"{request.command} did not pass",
                details={
                    "report": str(output),
                    "unconverted_call_sites": report["unconverted_call_sites"],
                },
            )
        return success_response(
            request,
            data=report,
            artifacts=[str(output)],
            limitations=[
                "Mock tests prove code contracts only; "
                "model quality and economics remain unevaluated."
            ],
        )
    except (ValueError, KeyError, OSError, StopIteration, subprocess.SubprocessError) as error:
        return failure_response(request, code="lifecycle_rejected", message=str(error))

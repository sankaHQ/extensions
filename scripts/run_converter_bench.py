# SPDX-License-Identifier: Apache-2.0
"""Check this converter checkout through its stdio contract and independent Bench.

Run from the extensions workspace after syncing its development dependencies and
Bench's frozen fixture environment with Python 3.12. Private fixtures are copied
only into temporary directories; the report contains outcomes and provenance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from sanka_extension_sdk import (
    ExtensionRequest,
    ExtensionResponse,
    decode_response,
    encode_request,
    encode_response,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "scripts" / "converter_bench.json"
EXTENSION = ROOT / "packages" / "sanka-extension-drf-to-fastapi"


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def converter_input_sha256() -> str:
    paths = {
        MANIFEST,
        Path(__file__).resolve(),
        ROOT / "uv.lock",
        EXTENSION / "extension.json",
        *EXTENSION.joinpath("src").rglob("*.py"),
        *ROOT.joinpath("packages/sanka-extension-sdk/src").rglob("*.py"),
    }
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def clean_environment() -> dict[str, str]:
    allowed = {"PATH", "LANG", "LC_ALL", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR"}
    return {
        **{key: value for key, value in os.environ.items() if key in allowed},
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def benchmark_environment(bench: Path) -> dict[str, str]:
    return {**clean_environment(), "PYTHONPATH": str(bench / "src")}


def preflight(bench: Path, manifest: dict[str, Any]) -> Path:
    expected = manifest["benchmark_revision"]
    if git(bench, "rev-parse", "HEAD") != expected:
        raise ValueError(f"Bench must be checked out at reviewed revision {expected}")
    if git(
        bench,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        "src",
        "tasks",
        "pyproject.toml",
        "uv.lock",
    ):
        raise ValueError("Bench evaluator, fixtures and dependency lock must be clean")
    tasks = {path.parent.name for path in (bench / "tasks" / "drf-fastapi").glob("*/task.yaml")}
    if tasks != set(manifest["route_envelope"]):
        raise ValueError("Bench tasks differ from the reviewed route envelope")
    python = bench / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    version = subprocess.check_output(
        [str(python), "-c", "import sys; print('.'.join(map(str, sys.version_info[:2])))"],
        env=clean_environment(),
        text=True,
    ).strip()
    if version != "3.12":
        raise ValueError("Sync Bench's frozen fixture environment with --python 3.12")
    module = subprocess.check_output(
        [str(python), "-P", "-c", "import sanka_bench; print(sanka_bench.__file__)"],
        cwd=bench,
        env=benchmark_environment(bench),
        text=True,
    ).strip()
    if Path(module).resolve() != bench / "src/sanka_bench/__init__.py":
        raise ValueError("Evaluator import does not resolve to the pinned Bench source")
    return python


def invoke(
    python: Path, request: ExtensionRequest, environment: dict[str, str]
) -> ExtensionResponse:
    outcome = subprocess.run(
        [str(python), "-m", "sanka_extension_drf_to_fastapi"],
        input=json.dumps(encode_request(request)),
        cwd=request.project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    response = decode_response(json.loads(outcome.stdout))
    if (
        response.request_id != request.request_id
        or response.command != request.command
        or response.extension_id != request.extension_id
        or response.extension_version != request.extension_version
        or outcome.returncode != (0 if response.outcome == "success" else 1)
    ):
        raise ValueError("Extension response identity or exit status does not match the request")
    return response


def require_success(response: ExtensionResponse) -> None:
    if response.outcome != "success":
        raise ValueError(f"{response.command}: {response.error}")


def readiness_error(task: str, plan: dict[str, Any], envelope: dict[str, Any]) -> str | None:
    if task not in envelope:
        return "task has no reviewed route envelope"
    minimum, eligible = envelope[task]
    native = plan.get("native_routes")
    if plan.get("native_eligible_routes") != eligible:
        return f"eligible route count changed from {eligible}"
    if not isinstance(native, int) or isinstance(native, bool) or not minimum <= native <= eligible:
        return f"native route count is outside reviewed floor {minimum}/{eligible}"
    if plan.get("readiness") != native / eligible:
        return "readiness differs from native/eligible route counts"
    return None


def evaluation_error(
    native: int,
    eligible: int,
    result: dict[str, Any],
    baseline: dict[str, Any] | None = None,
) -> str | None:
    gates = result.get("hard_gates", {})
    if result.get("status") == "invalid" or gates.get("source_qualified") is not True:
        return "benchmark source is not qualified"
    if native == eligible:
        if result.get("fully_migrated") is not True or not gates or not all(gates.values()):
            return "fully generatable candidate failed an independent migration gate"
    else:
        if baseline is None:
            return "partial candidate has no reviewed evaluation baseline"
        if any(gates.get(key) is not True for key in baseline["required_gates"]):
            return "partial candidate lost a previously passing independent gate"
        metrics = result.get("metrics", {})
        for key, (minimum, total) in baseline["metrics"].items():
            measured = metrics.get(key, {})
            passed = measured.get("passed")
            if (
                measured.get("total") != total
                or not isinstance(passed, int)
                or isinstance(passed, bool)
                or not minimum <= passed <= total
            ):
                return f"partial candidate regressed below {key} floor {minimum}/{total}"
    return None


def run_task(task: str, bench: Path, python: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"converter-{task}-") as temporary:
        root = Path(temporary).resolve()
        # Even the source-side regression commands run against a disposable task.
        task_root = root / "task"
        shutil.copytree(
            bench / "tasks" / "drf-fastapi" / task,
            task_root,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        project = root / "project"
        shutil.copytree(task_root / "source", project)
        artifacts = root / "extension-artifacts"
        artifacts.mkdir()
        candidate = root / "candidate"
        extension_manifest = (EXTENSION / "extension.json").read_bytes()
        identity = json.loads(extension_manifest)
        environment = clean_environment()
        # Use the benchmark's pinned Django/DRF environment for both source
        # inspection and evaluation, loading only this checkout's extension/SPI.
        environment["PYTHONSAFEPATH"] = "1"
        environment["PYTHONPATH"] = os.pathsep.join(
            [
                str(EXTENSION / "src"),
                str(ROOT / "packages" / "sanka-extension-sdk" / "src"),
            ]
        )
        request = ExtensionRequest(
            request_id=f"{task}-scan",
            command="scan",
            project_root=str(project),
            artifact_root=str(artifacts),
            extension_id=identity["id"],
            extension_version=identity["version"],
            manifest_digest=hashlib.sha256(extension_manifest).hexdigest(),
            fingerprint={},
            configuration={},
            prior_artifacts=(),
            reviewed_plan_hash=None,
        )
        scan = invoke(python, request, environment)
        require_success(scan)
        request = replace(
            request,
            request_id=f"{task}-plan",
            command="plan",
            prior_artifacts=scan.artifacts,
            configuration={
                "generation": "minimal",
                "output": str(root / "generated"),
                "strategy": "native",
                "package_manager": "pip",
                "orm": "tortoise",
            },
        )
        plan_response = invoke(python, request, environment)
        require_success(plan_response)
        plan = plan_response.data
        if error := readiness_error(task, plan, manifest["route_envelope"]):
            raise ValueError(error)
        native, eligible = plan["native_routes"], plan["native_eligible_routes"]
        assert isinstance(native, int) and isinstance(eligible, int)
        # The host reviews this response, while extension_plan_hash selects the
        # converter's plan. Never treat a core CLI plan file as an extension plan.
        review_hash = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(encode_response(plan_response), sort_keys=True).encode()
            ).hexdigest()
        )
        request = replace(
            request,
            request_id=f"{task}-apply",
            command="apply",
            prior_artifacts=plan_response.artifacts,
            reviewed_plan_hash=review_hash,
            configuration={
                **request.configuration,
                "extension_plan_hash": plan["plan_hash"],
                "bench_candidate": str(candidate),
            },
        )
        applied = invoke(python, request, environment)
        refused = native == 0 or native / eligible < 0.5
        if refused:
            if (
                applied.error is None
                or applied.error.code != "SANKA_EXTENSION_READINESS"
                or str(candidate) not in applied.artifacts
                or not (candidate / "GAP-REPORT.md").is_file()
                or (candidate / "candidate.yaml").exists()
            ):
                raise ValueError("default apply did not refuse safely with a gap report")
        else:
            require_success(applied)
        summary: dict[str, Any] = {
            "task": task,
            "native_routes": native,
            "eligible_routes": eligible,
            "default_refusal_verified": refused,
            "plan_hash": plan["plan_hash"],
        }
        if native == 0:
            return {**summary, "outcome": "expected_refusal"}
        if refused:
            applied = invoke(
                python,
                replace(
                    request,
                    request_id=f"{task}-partial-apply",
                    configuration={**request.configuration, "min_readiness": 0},
                ),
                environment,
            )
            require_success(applied)
        if not (candidate / "GAP-REPORT.md").is_file():
            raise ValueError("generated candidate omitted its gap disclosure")
        result_path = root / "evaluation.json"
        evaluated = subprocess.run(
            [
                str(python),
                "-P",
                "-m",
                "sanka_bench.cli",
                "evaluate",
                "--runner",
                "local",
                "--task",
                str(task_root),
                "--candidate",
                str(candidate),
                "--output",
                str(result_path),
            ],
            cwd=bench,
            env=benchmark_environment(bench),
            capture_output=True,
            text=True,
            timeout=1200,
            check=False,
        )
        if evaluated.returncode != 0 or not result_path.is_file():
            raise ValueError(
                f"evaluator failed: {evaluated.stderr.strip() or evaluated.stdout.strip()}"
            )
        result = json.loads(result_path.read_text())
        if error := evaluation_error(
            native, eligible, result, manifest["partial_evaluation_floors"].get(task)
        ):
            return {**summary, "outcome": "failed", "error": error, "evaluation": result}
        return {
            **summary,
            "outcome": "fully_migrated" if native == eligible else "partial",
            "evaluation": result,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bench-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output = args.output.resolve()
    if args.output.is_relative_to(ROOT):
        parser.error(
            "private evaluation reports must be written outside the public extensions repo"
        )
    report: dict[str, Any] = {"schema_version": "sanka-converter-regression/v1", "tasks": []}
    try:
        manifest = json.loads(MANIFEST.read_text())
        bench = args.bench_dir.resolve(strict=True)
        python = preflight(bench, manifest)
        report.update(
            {
                "extensions_revision": git(ROOT, "rev-parse", "HEAD"),
                "extensions_dirty": bool(git(ROOT, "status", "--porcelain")),
                "benchmark_revision": manifest["benchmark_revision"],
                "converter_input_sha256": converter_input_sha256(),
                "benchmark_python": subprocess.check_output(
                    [str(python), "-c", "import sys; print(sys.version)"],
                    env=clean_environment(),
                    text=True,
                ).strip(),
            }
        )
        for task in sorted(manifest["route_envelope"]):
            try:
                result = run_task(task, bench, python, manifest)
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                result = {"task": task, "outcome": "failed", "error": str(error)}
            report["tasks"].append(result)
            print(
                f"{task}: {result['outcome']}"
                + (f" ({result['error'][:500]})" if "error" in result else ""),
                flush=True,
            )
        report["passed"] = all(row["outcome"] != "failed" for row in report["tasks"])
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        report.update({"passed": False, "error": str(error)})
        print(str(error), file=sys.stderr)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

# SPDX-License-Identifier: Apache-2.0
"""Translate retired framework CLI arguments into extension requests for parity tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from sanka_extension_drf_to_fastapi.adapter import handle
from sanka_extension_sdk import ExtensionRequest, JsonValue, encode_request, encode_response


def _value(arguments: list[str], name: str, default: str | None = None) -> str | None:
    try:
        return arguments[arguments.index(name) + 1]
    except (ValueError, IndexError):
        return default


def _request(arguments: list[str], cwd: Path) -> ExtensionRequest:
    command = arguments[0]
    root = Path(
        arguments[1] if command in {"scan", "plan"} else _value(arguments, "--root", str(cwd))
    ).resolve()
    configuration: dict[str, JsonValue] = {}
    if command == "scan":
        if settings := _value(arguments, "--settings"):
            configuration["settings_module"] = settings
    elif command == "plan":
        configuration = {
            "output": _value(arguments, "--output", ".sanka/output/fastapi"),
            "strategy": _value(arguments, "--strategy", "native"),
            "generation": _value(arguments, "--generation", "minimal"),
            "package_manager": _value(arguments, "--package-manager", "uv"),
        }
        if orm := _value(arguments, "--orm"):
            configuration["orm"] = orm
    elif command == "apply":
        plan_hash = _value(arguments, "--plan-hash")
        if plan_hash is not None:
            configuration["extension_plan_hash"] = plan_hash
        if output := _value(arguments, "--output"):
            configuration["output"] = output
        if orm := _value(arguments, "--orm"):
            configuration["orm"] = orm
        if readiness := _value(arguments, "--min-readiness"):
            configuration["min_readiness"] = float(readiness)
        if candidate := _value(arguments, "--bench-candidate"):
            configuration["bench_candidate"] = candidate
        configuration["force"] = "--force" in arguments
        configuration["gap_report_only"] = "--gap-report-only" in arguments
    elif command == "test":
        if output := _value(arguments, "--output"):
            configuration["output"] = output
    elif command == "verify":
        configuration["no_http"] = "--no-http" in arguments
        if output := _value(arguments, "--output"):
            configuration["output"] = output
        if cases := _value(arguments, "--cases"):
            configuration["cases"] = cases
    return ExtensionRequest(
        request_id="parity-test",
        command=command,
        project_root=str(root),
        artifact_root=str((root / ".sanka").resolve()),
        extension_id="sanka/drf-to-fastapi",
        extension_version="0.1.0a1",
        manifest_digest="0" * 64,
        fingerprint={},
        configuration=configuration,
        prior_artifacts=(),
        reviewed_plan_hash="sha256:core-plan" if command == "apply" else None,
    )


def _legacy_output(payload: dict[str, Any]) -> str:
    data = payload.get("data") or (payload.get("error") or {}).get("details") or {}
    command = payload.get("command")
    if command == "scan":
        routes = data.get("routes", [])
        custom = sum(
            route.get("operation")
            not in {"list", "create", "retrieve", "update", "partial_update", "destroy"}
            and "ViewSet" in str(route.get("view", ""))
            for route in routes
        )
        skipped = data.get("skipped_routes", [])
        return "\n".join(
            [
                "Django REST Framework (DRF)",
                f"{len(routes)} endpoints",
                f"{len(data.get('serializers', []))} serializers",
                f"{len(data.get('models', []))} models",
                f"{len(data.get('permissions', []))} permissions",
                f"{custom} custom actions",
                "Not scanned (non-DRF views): "
                + ", ".join(item.get("pattern", "") for item in skipped),
                "next: sanka plan --to fastapi",
            ]
        )
    if command == "plan":
        mode = data.get("mode")
        label = "Bridge generation" if mode == "compatibility" else "Native migration"
        readiness = float(data.get("readiness", 0))
        reasons = sorted(
            {
                f"{reason.get('code')} ({reason.get('feature')})"
                for route in data.get("routes", [])
                for reason in route.get("adaptation_reasons", [])
            }
        )
        return (
            f"DRF → FastAPI Migration Plan ({mode})\n"
            f"{label} readiness: {readiness:.0%}\n"
            f"Needs adaptation\n  {data.get('needs_adaptation_routes', 0)} endpoints\n"
            f"Format-suffix aliases dropped: {data.get('dropped_alias_routes', 0)}\n"
            + "\n".join(reasons)
        )
    if command == "apply":
        if payload.get("outcome") == "success" and "output" in data:
            kind = "native FastAPI" if data.get("mode") == "native" else "FastAPI"
            return (
                f"generated {data.get('routes_generated')} {kind} routes in {data.get('output')}\n"
                "next: sanka test"
            )
        report = data.get("gap_report", "")
        readiness = float(data.get("readiness", 0))
        message = (
            (payload.get("error") or {})
            .get("message", "")
            .replace("below min_readiness", "below --min-readiness")
        )
        return f"native readiness: {readiness:.0%}\n{message}\ngap report written to {report}"
    if command == "test":
        status = "OK" if payload.get("outcome") == "success" else "FAILED"
        lines = [f"Generated API tests: {status}"]
        for key, label in (
            ("environment", "Generated environment"),
            ("python", "Generated Python"),
            ("pyproject", "Dependency metadata"),
            ("lockfile", "Locked dependencies"),
        ):
            if data.get(key):
                lines.append(f"{label}: {data[key]}")
        if status == "OK":
            lines.append("next: sanka verify")
        return "\n".join(lines)
    if command == "verify":
        paths = data.get("paths", {})
        routes = data.get("routes", {})
        http = data.get("http", {})
        lines = [
            "Verified paths",
            f"Source app:    {paths.get('source')}",
            f"Scan:          {paths.get('scan')}",
            f"Plan:          {paths.get('plan')}",
            f"Generated app: {paths.get('generated')}",
            f"Manifest:      {paths.get('manifest')}",
            f"Dependencies:  {paths.get('pyproject')}",
            f"Environment:   {paths.get('environment')}",
            f"Python:        {paths.get('python')}",
            f"Lockfile:      {paths.get('lockfile')}",
            "Generated Python checked",
        ]
        lines.extend(f"  - {name}" for name in data.get("generated_files", []))
        lines.extend(
            [
                f"{routes.get('generated', 0)} / {routes.get('planned', 0)} generated",
                "generated route declarations match the reviewed plan ✓",
                "generated Python files exist and compile ✓",
                "manifest matches the current scan and reviewed plan ✓",
                "generated serving path has no Django imports ✓",
                "automatic coverage is limited to parameter-free GET/HEAD routes",
                "compared status, content type, body, Allow, Location, and WWW-Authenticate",
                (
                    f"{http.get('passed', 0)} / {http.get('probed', 0)} "
                    "source-vs-generated probes matched"
                    if http.get("enabled")
                    else "HTTP verification skipped with --no-http"
                ),
            ]
        )
        if routes.get("needs_adaptation"):
            lines.append("Needs adaptation")
        label = (
            "Compatibility bridge" if data.get("mode") == "compatibility" else "Native migration"
        )
        state = "complete" if payload.get("outcome") == "success" else "FAILED"
        lines.append(f"{label} verification: {state}")
        return "\n".join(lines)
    return json.dumps(payload, sort_keys=True)


def main(arguments: list[str]) -> int:
    if arguments[0] == "apply" and _value(arguments, "--plan-hash") is None:
        sys.stderr.write("--plan-hash is required\n")
        raise SystemExit(2)
    response = handle(_request(arguments, Path.cwd()))
    payload = encode_response(response)
    sys.stdout.write(_legacy_output(payload) + "\n")
    if response.error is not None:
        sys.stderr.write(response.error.message + "\n")
    return 0 if response.outcome == "success" else 1


def run_cli(arguments: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    env.pop("SANKA_TEST_DB", None)
    request = _request(arguments, cwd)
    completed = subprocess.run(
        [sys.executable, "-m", "sanka_extension_drf_to_fastapi"],
        cwd=cwd,
        env=env,
        input=json.dumps(encode_request(request)),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    payload = json.loads(completed.stdout)
    stdout = (
        json.dumps(payload.get("data", payload), sort_keys=True)
        if "--json" in arguments
        else _legacy_output(payload)
    )
    error = payload.get("error") or {}
    stderr = completed.stderr or (str(error.get("message", "")) + "\n" if error else "")
    return subprocess.CompletedProcess(
        completed.args,
        completed.returncode,
        stdout=stdout,
        stderr=stderr,
    )

# SPDX-License-Identifier: Apache-2.0
"""Run a workspace code extension through its sanka-extension/v1 stdio protocol.

The Sanka CLI installs extensions from a marketplace snapshot. Extensions that are
still experimental are not in that catalog, so this script speaks the same JSON
protocol the CLI would use, against the extension installed in this workspace:

    uv run python scripts/run_extension.py sanka/typescript-to-rust scan --project ~/app
    uv run python scripts/run_extension.py sanka/typescript-to-rust plan --project ~/app \\
        --config database_layer=sqlx
    uv run python scripts/run_extension.py sanka/typescript-to-rust apply --project ~/app \\
        --config database_layer=sqlx --plan-hash sha256:...   # the hash plan printed

``apply`` keeps the CLI's review semantics: the hash you pass must equal the saved
plan's hash, and the extension recomputes the plan before writing anything.
Artifacts live under ``<project>/.sanka/<extension name>/``. The response summary
goes to stdout; ``--json`` prints the raw response document instead.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import subprocess
import sys
import uuid
from pathlib import Path

SCHEMA_VERSION = "sanka-extension/v1"
COMMANDS = ("scan", "plan", "apply", "test", "verify")


def extension_module(extension_id: str) -> tuple[str, str]:
    """Return the (module, distribution) names for ``sanka/<name>``."""
    prefix, _, name = extension_id.partition("/")
    if prefix != "sanka" or not name or "/" in name:
        raise SystemExit(f"extension ids look like sanka/<name>, not {extension_id!r}")
    module = "sanka_extension_" + name.replace("-", "_")
    return module, f"sanka-extension-{name}"


def build_request(
    extension_id: str,
    version: str,
    command: str,
    project: Path,
    artifact_root: Path,
    configuration: dict[str, str],
    plan_hash: str | None,
) -> dict[str, object]:
    if command == "apply":
        saved = artifact_root / "plan.json"
        if plan_hash is None:
            raise SystemExit("apply requires --plan-hash with the exact hash printed by plan")
        if not saved.is_file():
            raise SystemExit(f"run plan first; {saved} does not exist")
        current = json.loads(saved.read_text(encoding="utf-8")).get("plan_hash")
        if current != plan_hash:
            raise SystemExit(f"the saved plan hash is {current}; review it and pass that hash")
        configuration = {**configuration, "extension_plan_hash": plan_hash}
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": uuid.uuid4().hex,
        "command": command,
        "project_root": str(project),
        "artifact_root": str(artifact_root),
        "extension": {"id": extension_id, "version": version, "manifest_digest": "0" * 64},
        "fingerprint": {},
        "configuration": configuration,
        "prior_artifacts": [],
        "reviewed_plan_hash": plan_hash if command == "apply" else None,
    }


def summarize(command: str, response: dict[str, object]) -> list[str]:
    data = response.get("data")
    data = data if isinstance(data, dict) else {}
    lines = [f"{command}: {response.get('outcome')}"]
    error = response.get("error")
    if isinstance(error, dict):
        lines.append(f"  {error.get('code')}: {error.get('message')}")
    capture = data.get("capture") if isinstance(data.get("capture"), dict) else data
    if command in {"scan", "plan"} and isinstance(capture, dict):
        for key in ("routes", "screens", "models"):
            if isinstance(capture.get(key), list):
                lines.append(f"  {key}: {len(capture[key])}")
        if "readiness" in capture:
            lines.append(f"  readiness: {capture['readiness']}")
        for gap in capture.get("gaps", []) if isinstance(capture.get("gaps"), list) else []:
            lines.append(f"  gap: {gap}")
    if command == "plan":
        lines.append(f"  plan_hash: {data.get('plan_hash')}")
        files = data.get("files")
        lines.append(f"  generated files: {len(files) if isinstance(files, dict) else 0}")
        for item in (
            data.get("dispositions", []) if isinstance(data.get("dispositions"), list) else []
        ):
            if isinstance(item, dict) and item.get("disposition") != "native-screen":
                reasons = item.get("adaptation_reasons")
                lines.append(f"  {item.get('module')}: {item.get('disposition')} {reasons}")
    elif command == "apply":
        lines.append(f"  output: {data.get('output')}")
    elif command in {"test", "verify"}:
        lines.append(f"  ok: {data.get('ok')}")
        comparison = data.get("comparison")
        steps = comparison.get("steps") if isinstance(comparison, dict) else None
        for step in steps if isinstance(steps, list) else []:
            if isinstance(step, dict):
                for problem in step.get("problems", []):
                    lines.append(f"  {step.get('id')}: {problem}")
    for artifact in (
        response.get("artifacts", []) if isinstance(response.get("artifacts"), list) else []
    ):
        lines.append(f"  artifact: {artifact}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("extension_id", help="for example sanka/typescript-to-rust")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--project", required=True, type=Path, help="the source project root")
    parser.add_argument(
        "--artifact-root", type=Path, help="defaults to <project>/.sanka/<extension name>"
    )
    parser.add_argument("--config", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--plan-hash", help="the exact plan hash to apply")
    parser.add_argument("--json", action="store_true", help="print the raw response document")
    arguments = parser.parse_args(argv)
    module, distribution = extension_module(arguments.extension_id)
    try:
        importlib.import_module(module)
        version = importlib.metadata.version(distribution)
    except (ImportError, importlib.metadata.PackageNotFoundError):
        raise SystemExit(
            f"{distribution} is not installed in this environment; run "
            "`uv sync --frozen --all-packages` in the extensions checkout"
        ) from None
    configuration: dict[str, str] = {}
    for item in arguments.config:
        key, separator, value = item.partition("=")
        if not separator or not key:
            raise SystemExit(f"--config expects KEY=VALUE, not {item!r}")
        configuration[key] = value
    project = arguments.project.resolve()
    if not project.is_dir():
        raise SystemExit(f"{project} is not a directory")
    name = arguments.extension_id.partition("/")[2]
    artifact_root = (arguments.artifact_root or project / ".sanka" / name).resolve()
    request = build_request(
        arguments.extension_id,
        version,
        arguments.command,
        project,
        artifact_root,
        configuration,
        arguments.plan_hash,
    )
    process = subprocess.run(
        [sys.executable, "-m", module],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=False,
    )
    if process.stderr.strip():
        print(process.stderr.strip(), file=sys.stderr)
    try:
        response = json.loads(process.stdout)
    except ValueError:
        print(process.stdout, file=sys.stderr)
        raise SystemExit(
            f"the extension returned no JSON response (exit {process.returncode})"
        ) from None
    if arguments.json:
        print(json.dumps(response, indent=2, sort_keys=True))
    else:
        print("\n".join(summarize(arguments.command, response)))
    return 0 if response.get("outcome") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())

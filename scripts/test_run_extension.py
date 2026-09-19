# SPDX-License-Identifier: Apache-2.0
"""The protocol runner drives a workspace extension exactly like the CLI would."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sanka_ts_capture import MIN_NODE_MAJOR, TypeScriptDriverError, node_executable, node_version

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_extension.py"
FIXTURE = (
    ROOT / "packages" / "sanka-extension-typescript-to-rust" / "tests" / "fixtures" / "pg-writes"
)


def _node_ready() -> bool:
    try:
        return node_version(node_executable())[0] >= MIN_NODE_MAJOR
    except TypeScriptDriverError:
        return False


def run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def test_rejects_malformed_invocations(tmp_path: Path) -> None:
    assert run("typescript-to-rust", "scan", "--project", str(tmp_path)).returncode != 0
    result = run(
        "sanka/typescript-to-rust", "scan", "--project", str(tmp_path), "--config", "nonsense"
    )
    assert result.returncode != 0 and "KEY=VALUE" in result.stderr
    result = run("sanka/typescript-to-rust", "apply", "--project", str(tmp_path))
    assert result.returncode != 0 and "--plan-hash" in result.stderr
    result = run("sanka/does-not-exist", "scan", "--project", str(tmp_path))
    assert result.returncode != 0 and "not installed" in result.stderr


@pytest.mark.skipif(not _node_ready(), reason="requires Node.js 20 or later")
def test_scan_plan_apply_through_the_protocol(tmp_path: Path) -> None:
    project = tmp_path / "app"
    shutil.copytree(FIXTURE, project)
    scanned = run(
        "sanka/typescript-to-rust",
        "scan",
        "--project",
        str(project),
        "--config",
        "database_layer=sqlx",
    )
    assert scanned.returncode == 0, scanned.stdout + scanned.stderr
    assert "scan: success" in scanned.stdout and "routes: 6" in scanned.stdout
    planned = run(
        "sanka/typescript-to-rust",
        "plan",
        "--project",
        str(project),
        "--config",
        "database_layer=sqlx",
        "--json",
    )
    assert planned.returncode == 0, planned.stdout + planned.stderr
    plan_hash = json.loads(planned.stdout)["data"]["plan_hash"]
    assert (project / ".sanka" / "typescript-to-rust" / "plan.json").is_file()
    wrong = run(
        "sanka/typescript-to-rust",
        "apply",
        "--project",
        str(project),
        "--config",
        "database_layer=sqlx",
        "--plan-hash",
        "sha256:" + "0" * 64,
    )
    assert wrong.returncode != 0 and "saved plan hash" in wrong.stderr
    applied = run(
        "sanka/typescript-to-rust",
        "apply",
        "--project",
        str(project),
        "--config",
        "database_layer=sqlx",
        "--plan-hash",
        plan_hash,
    )
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert (project / ".sanka" / "typescript-to-rust" / "rust" / "Cargo.toml").is_file()
    # Without fixture databases the replay explains what it needs instead of guessing.
    tested = run(
        "sanka/typescript-to-rust",
        "test",
        "--project",
        str(project),
        "--config",
        "database_layer=sqlx",
    )
    assert tested.returncode != 0 and "SANKA_RUST_TARGET_TEST_DATABASE_URL" in tested.stdout

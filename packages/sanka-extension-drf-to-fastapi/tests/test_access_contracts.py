# SPDX-License-Identifier: Apache-2.0
"""Membership access must fail closed, including cross-parent and anonymous requests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures/drf_membership_project"


@pytest.mark.parametrize("engine", ["tortoise", "sqlalchemy"])
def test_access_capture_and_native_request_gates(tmp_path: Path, engine: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("access_probe.py")),
            str(FIXTURE),
            str(tmp_path),
            json.dumps(sys.path),
            engine,
        ],
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") != "true" or sys.platform != "linux",
    reason="Full conversion acceptance runs in disposable Linux CI.",
)
def test_generated_membership_application_parity(tmp_path: Path) -> None:
    from test_native_fastapi import _generate

    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)
    output = _generate(project)
    database = tmp_path / "database"
    database.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("access_probe.py")),
            str(project),
            str(database),
            json.dumps(sys.path),
            "tortoise",
            str(output),
        ],
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for name in ("app.py", "sanka_native.py", "sanka_store.py", "models.py"):
        source = (output / name).read_text()
        assert "import django" not in source and "rest_framework" not in source

# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures: the pinned Express and pg toolset used to replay the source."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent / "node-tools"


@pytest.fixture
def node_tools(monkeypatch: pytest.MonkeyPatch) -> Path:
    modules = TOOLS / "node_modules"
    if not all((modules / name / "package.json").is_file() for name in ("express", "pg")):
        if os.getenv("SANKA_NODE_TESTS") != "1":
            pytest.skip("set SANKA_NODE_TESTS=1 to install the pinned Express and pg toolset")
        npm = shutil.which("npm")
        assert npm, "npm is required to install the pinned Express and pg toolset"
        subprocess.run(
            [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=TOOLS,
            check=True,
            capture_output=True,
            timeout=600,
        )
    monkeypatch.setenv("SANKA_NODE_TOOLS", str(modules))
    return modules

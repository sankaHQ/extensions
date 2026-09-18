# SPDX-License-Identifier: Apache-2.0
"""Generated Fiber process boundary: configuration, build, and shutdown wiring."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from test_golang_reads import read_source
from test_golang_schema import generate
from test_python_to_golang import apply, source


@pytest.mark.parametrize("database", [False, True])
def test_fiber_runtime_is_generated_and_builds(tmp_path: Path, database: bool) -> None:
    if database:
        output = generate(tmp_path, "flask", "fiber", app_source=read_source("flask"))
    else:
        (tmp_path / "app.py").write_text(source("flask"))
        output = apply(tmp_path, "flask", "fiber")
    command = output / "cmd/api/main.go"
    runtime_test = output / "cmd/api/main_test.go"
    assert command.is_file()
    assert runtime_test.is_file()
    text = command.read_text()
    assert "signal.NotifyContext" in text
    assert "GracefulContext:" in text
    assert "ShutdownTimeout:" in text
    assert "DATABASE_URL is required" in text if database else "DATABASE_URL" not in text
    assert "PORT must be an integer between 1 and 65535" in text
    app = (output / "app.go").read_text()
    assert "ReadTimeout:" in app
    assert "WriteTimeout:" in app
    assert "IdleTimeout:" in app
    assert "BodyLimit:" in app
    if os.getenv("SANKA_GO_TESTS") == "1":
        environment = os.environ | {
            "GOTOOLCHAIN": "local",
            "GOWORK": "off",
            "GOMAXPROCS": "2",
        }
        commands = [
            ["go", "test", "-mod=readonly", "-p=2", "./..."],
            ["go", "vet", "-mod=readonly", "./..."],
            ["go", "build", "-mod=readonly", "-o", str(tmp_path / "api"), "./cmd/api"],
        ]
        for command_line in commands:
            result = subprocess.run(
                command_line,
                cwd=output,
                env=environment,
                capture_output=True,
                text=True,
                timeout=180,
            )
            assert result.returncode == 0, result.stdout + result.stderr


def test_other_frameworks_do_not_claim_a_runtime(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(source("flask"))
    output = apply(tmp_path, "flask", "chi")
    assert not (output / "cmd/api").exists()

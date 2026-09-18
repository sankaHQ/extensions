# SPDX-License-Identifier: Apache-2.0
"""Generated Go process boundaries: configuration, build, and shutdown wiring."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from sanka_extension_python_to_golang.capture import TARGETS
from test_golang_reads import read_source
from test_golang_schema import generate
from test_python_to_golang import apply, source


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("database", [False, True])
def test_runtime_is_generated_and_builds(tmp_path: Path, target: str, database: bool) -> None:
    if database:
        output = generate(tmp_path, "flask", target, app_source=read_source("flask"))
    else:
        (tmp_path / "app.py").write_text(source("flask"))
        output = apply(tmp_path, "flask", target)
    command = output / "cmd/api/main.go"
    runtime_test = output / "cmd/api/main_test.go"
    assert command.is_file()
    assert runtime_test.is_file()
    text = command.read_text()
    assert "signal.NotifyContext" in text
    if target == "fiber":
        assert "GracefulContext:" in text
        assert "ShutdownTimeout:" in text
    else:
        assert "http.MaxBytesHandler" in text
        assert "ReadHeaderTimeout:" in text
        assert "server.Shutdown(shutdownCtx)" in text
    assert "DATABASE_URL is required" in text if database else "DATABASE_URL" not in text
    assert "PORT must be an integer between 1 and 65535" in text
    limits = (output / "app.go").read_text() if target == "fiber" else text
    assert "ReadTimeout:" in limits
    assert "WriteTimeout:" in limits
    assert "IdleTimeout:" in limits
    assert "BodyLimit:" in limits if target == "fiber" else "MaxBytesHandler" in limits
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

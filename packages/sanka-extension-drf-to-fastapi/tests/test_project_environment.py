# SPDX-License-Identifier: Apache-2.0
"""The public quickstart keeps Django outside the CLI and extension installs."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

from sanka_extension_drf_to_fastapi.project_environment import (
    _BOOTSTRAP,
    MINIMUM_PYTHON,
    PROJECT_PYTHON_PLACEHOLDER,
    python_mismatch_responses,
)
from sanka_extensions.code import ExtensionRequest, encode_request


def _environment(path: Path, packages: tuple[str, ...]) -> Path:
    venv.EnvBuilder(symlinks=True).create(path)
    python = path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    site = Path(
        subprocess.check_output(
            [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
            text=True,
        ).strip()
    )
    for name in packages:
        module = importlib.import_module(name)
        assert module.__file__ is not None
        source = Path(module.__file__).parent
        shutil.copytree(source, site / name, ignore=shutil.ignore_patterns("__pycache__"))
    return python


def _request(project: Path, command: str = "scan") -> dict:
    return encode_request(
        ExtensionRequest(
            request_id="quickstart",
            command=command,
            project_root=str(project.resolve()),
            artifact_root=str((project / ".sanka").resolve()),
            extension_id="sanka/drf-to-fastapi",
            extension_version="0.1.0a9",
            manifest_digest="0" * 64,
            fingerprint={},
            configuration={
                "generation": "minimal",
                "output": ".sanka/output/fastapi",
                "package_manager": "uv",
                "strategy": "native",
                # The replay must not depend on pipe capacity or argv limits.
                "padding": "x" * 200_000,
            },
            prior_artifacts=(),
            reviewed_plan_hash=None,
        )
    )


def test_scan_and_plan_use_only_project_django_without_pythonpath(tmp_path: Path) -> None:
    project = tmp_path / "source with spaces"
    shutil.copytree(Path(__file__).parent / "fixtures/drf_project", project)
    source_python = _environment(
        project / ".venv", ("django", "rest_framework", "asgiref", "sqlparse")
    )
    extension_python = _environment(
        tmp_path / "isolated extension",
        (
            "sanka_extension_drf_to_fastapi",
            "sanka_drf_replay",
            "sanka_extensions",
            "sanka_extension_sdk",
            "sanka_connector",
            "sanka_code_migration",
        ),
    )
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    # An unrelated activated venv must not take precedence over this project.
    environment["VIRTUAL_ENV"] = str(tmp_path / "unrelated")
    for python, missing in (
        (extension_python, "django"),
        (source_python, "sanka_extension_drf_to_fastapi"),
    ):
        subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                f"import importlib.util; assert importlib.util.find_spec('{missing}') is None",
            ],
            env=environment,
            check=True,
        )
    # A project dependency cannot replace the reviewed extension's entry point.
    fake = (
        Path(
            subprocess.check_output(
                [
                    str(source_python),
                    "-I",
                    "-c",
                    "import sysconfig; print(sysconfig.get_path('purelib'))",
                ],
                text=True,
            ).strip()
        )
        / "sanka_extension_drf_to_fastapi"
    )
    fake.mkdir(parents=True)
    (fake / "__init__.py").write_text("raise RuntimeError('unreviewed extension selected')\n")
    for command in ("scan", "plan"):
        result = subprocess.run(
            [str(extension_python), "-I", "-B", "-m", "sanka_extension_drf_to_fastapi"],
            input=json.dumps(_request(project, command)),
            text=True,
            capture_output=True,
            env=environment,
            cwd=project,
            timeout=30,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        response = json.loads(result.stdout)
        assert response["outcome"] == "success"
        assert response["request_id"] == "quickstart"
        assert response["command"] == command
        assert response["artifacts"]
    assert not list((tmp_path / "isolated extension").rglob("__pycache__"))
    scan = json.loads((project / ".sanka/scan.json").read_text())
    assert scan["routes"]
    assert (
        scan["python_version"]
        == subprocess.check_output(
            [str(source_python), "-c", "import platform; print(platform.python_version())"],
            text=True,
        ).strip()
    )


def test_bootstrap_reports_python_mismatch_before_loading_extension(tmp_path: Path) -> None:
    error = {"outcome": "error", "error": {"code": "SANKA_SOURCE_PYTHON_MISMATCH"}}
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _BOOTSTRAP,
            json.dumps({"python_version": [0, 0], "version_error": json.dumps(error) + "\n"}),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=10,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout) == error
    assert result.stderr == ""


def test_incomplete_project_environment_returns_protocol_error(tmp_path: Path) -> None:
    (tmp_path / ".venv").mkdir()
    result = subprocess.run(
        [sys.executable, "-m", "sanka_extension_drf_to_fastapi"],
        input=json.dumps(_request(tmp_path)),
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 1
    response = json.loads(result.stdout)
    assert response["request_id"] == "quickstart"
    assert "uv venv --python 3.12" in response["error"]["message"]


def _mismatch_configuration(python_version: list[int], minimum_python: list[int]) -> str:
    request = ExtensionRequest(
        request_id="mismatch",
        command="scan",
        project_root="/project",
        artifact_root="/project/.sanka",
        extension_id="sanka/drf-to-fastapi",
        extension_version="0.1.0a9",
        manifest_digest="0" * 64,
        fingerprint={},
        configuration={},
        prior_artifacts=(),
        reviewed_plan_hash=None,
    )
    return json.dumps(
        {
            "extension_paths": [],
            "python_version": python_version,
            "minimum_python": minimum_python,
            **python_mismatch_responses(request, "3.14"),
        }
    )


def _bootstrap_mismatch(configuration: str) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", _BOOTSTRAP, configuration],
        text=True,
        capture_output=True,
        timeout=30,
    )
    return result.returncode, result.stdout


def test_python_mismatch_recommends_reinstalling_the_cli_on_the_project_python() -> None:
    project_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    code, output = _bootstrap_mismatch(_mismatch_configuration([3, 99], list(MINIMUM_PYTHON)))
    assert code == 1
    document = json.loads(output)
    message = json.dumps(document)
    assert "SANKA_SOURCE_PYTHON_MISMATCH" in message
    assert PROJECT_PYTHON_PLACEHOLDER not in message
    assert f"uses Python {project_python}" in message
    assert "built with Python 3.14" in message
    assert f"uv tool install --python {project_python} --force sanka-cli" in message
    assert "sanka extension remove sanka/drf-to-fastapi" in message
    assert "sanka extension add sanka/drf-to-fastapi" in message


def test_python_below_minimum_recommends_recreating_the_project_venv() -> None:
    project_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    code, output = _bootstrap_mismatch(_mismatch_configuration([3, 99], [3, 99]))
    assert code == 1
    message = json.dumps(json.loads(output))
    assert "SANKA_SOURCE_PYTHON_MISMATCH" in message
    assert f"uses Python {project_python}" in message
    assert "requires Python 3.12 or newer" in message
    assert "uv venv --python 3.14 .venv" in message
    assert "uv tool install" not in message


def test_matching_python_skips_the_mismatch_document() -> None:
    configuration = json.loads(_mismatch_configuration(list(sys.version_info[:2]), [3, 12]))
    configuration["extension_paths"] = ["/nonexistent extension path"]
    code, output = _bootstrap_mismatch(json.dumps(configuration))
    # The bootstrap proceeds to import the extension, which this isolated
    # interpreter cannot find; the mismatch document must not be written.
    assert code != 0
    assert "SANKA_SOURCE_PYTHON_MISMATCH" not in output

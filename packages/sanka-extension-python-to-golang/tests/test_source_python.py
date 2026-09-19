# SPDX-License-Identifier: Apache-2.0
"""Source environments work without weakening isolated replay or extension locks."""

from __future__ import annotations

import os
import sys
import venv
from pathlib import Path

import pytest
from sanka_extension_python_to_golang.replay import _run, _source_python


def test_default_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANKA_GO_SOURCE_PYTHON", raising=False)
    assert _source_python() == sys.executable


@pytest.mark.parametrize("value", ["", "python3", "relative/bin/python", "/missing/python"])
def test_reject_invalid_source_interpreter(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SANKA_GO_SOURCE_PYTHON", value)
    with pytest.raises(ValueError, match="absolute executable Python"):
        _source_python()


def test_reject_directory_and_nonexecutable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "python"
    executable.write_text("not executable")
    for path in (tmp_path, executable):
        monkeypatch.setenv("SANKA_GO_SOURCE_PYTHON", str(path))
        with pytest.raises(ValueError, match="absolute executable Python"):
            _source_python()


def test_selected_venv_keeps_dependencies_and_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = tmp_path / "source-env"
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(environment)
    executable = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    site_packages = Path(
        _run(
            [str(executable), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
            tmp_path,
        ).strip()
    )
    (site_packages / "source_dependency.py").write_text("VALUE = 'selected source environment'\n")
    (tmp_path / "source_dependency.py").write_text(
        "raise RuntimeError('cwd or PYTHONPATH leaked')\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("SANKA_GO_SOURCE_PYTHON", str(executable))
    assert _source_python() == str(executable)
    observed = _run(
        [_source_python(), "-I", "-c", "import source_dependency; print(source_dependency.VALUE)"],
        tmp_path,
    )
    assert observed.strip() == "selected source environment"

# SPDX-License-Identifier: Apache-2.0
"""Run the locked extension with the source project's Python and dependencies."""

from __future__ import annotations

import json
import os
import sys
import sysconfig
import tempfile
from pathlib import Path

import sanka_connector
import sanka_drf_replay
import sanka_extension_drf_to_flask
import sanka_extension_sdk
import sanka_extensions
from sanka_extensions.code import ExtensionRequest, encode_response, failure_response

# Load the installed extension and its protocol before allowing project packages
# to resolve imports. Then prefer the source environment for Django/app imports.
# exec preserves the runtime's PID, timeout and bounded stdout/stderr pipes.
_BOOTSTRAP = """
import json, sys
configuration = json.loads(sys.argv[1])
if list(sys.version_info[:2]) != configuration["python_version"]:
    sys.stdout.write(configuration["version_error"])
    raise SystemExit(1)
project_paths = list(sys.path)
sys.path[:0] = configuration["extension_paths"]
from sanka_extension_drf_to_flask.__main__ import main
sys.path[:] = project_paths + configuration["extension_paths"]
raise SystemExit(main(project_environment=False))
"""


def use_project_environment(request: ExtensionRequest, document: str) -> None:
    environment = Path(request.project_root) / ".venv"
    if not environment.exists():
        return
    relative = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    python = environment / relative
    if not (environment / "pyvenv.cfg").is_file() or not python.is_file():
        raise ValueError(
            "The project's .venv is incomplete. Recreate it with "
            "`uv venv --python 3.12 .venv`, then install the project's requirements."
        )
    if Path(sys.prefix).resolve() == environment.resolve():
        return

    # Use package roots, not sys.path's working directory or another interpreter's
    # standard library. This also supports the repository's editable test installs.
    paths = [sysconfig.get_path("purelib"), sysconfig.get_path("platlib")]
    for package in (
        sanka_extension_drf_to_flask,
        sanka_drf_replay,
        sanka_extensions,
        sanka_extension_sdk,
        sanka_connector,
    ):
        assert package.__file__ is not None
        paths.append(str(Path(package.__file__).resolve().parent.parent))
    required_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    version_error = failure_response(
        request,
        code="SANKA_SOURCE_PYTHON_MISMATCH",
        message=(
            f"The project's .venv must use Python {required_python} to load the locked "
            f"extension dependencies. Recreate it with `uv venv --python {required_python} "
            ".venv`, then install the project's requirements."
        ),
    )
    configuration = json.dumps(
        {
            "extension_paths": list(dict.fromkeys(paths)),
            "python_version": list(sys.version_info[:2]),
            "version_error": json.dumps(encode_response(version_error)) + "\n",
        }
    )
    process_environment = dict(os.environ, VIRTUAL_ENV=str(environment))
    # A file avoids pipe capacity and command-line size limits for the request.
    # It is unlinked automatically and only its stdin descriptor survives exec.
    with tempfile.TemporaryFile() as replay:
        replay.write(document.encode("utf-8"))
        replay.seek(0)
        os.dup2(replay.fileno(), 0, inheritable=True)
        os.execve(
            python,
            [str(python), "-I", "-B", "-c", _BOOTSTRAP, configuration],
            process_environment,
        )

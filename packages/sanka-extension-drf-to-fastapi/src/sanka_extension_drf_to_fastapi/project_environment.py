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
import sanka_extension_drf_to_fastapi
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
    project_python = "%d.%d" % sys.version_info[:2]
    below_minimum = list(sys.version_info[:2]) < configuration.get("minimum_python", [3, 12])
    key = "unsupported_python_error" if below_minimum else "version_error"
    document = configuration.get(key) or configuration["version_error"]
    sys.stdout.write(document.replace("__PROJECT_PYTHON__", project_python))
    raise SystemExit(1)
project_paths = list(sys.path)
sys.path[:0] = configuration["extension_paths"]
from sanka_extension_drf_to_fastapi.__main__ import main
sys.path[:] = project_paths + configuration["extension_paths"]
raise SystemExit(main(project_environment=False))
"""

MINIMUM_PYTHON = (3, 12)
PROJECT_PYTHON_PLACEHOLDER = "__PROJECT_PYTHON__"


def python_mismatch_responses(request: ExtensionRequest, required_python: str) -> dict[str, str]:
    """Failure documents for a project .venv on another Python than the extension.

    Only the project interpreter knows its own version, so the bootstrap fills
    ``__PROJECT_PYTHON__`` before writing the document. The extension environment
    runs on the interpreter that runs the sanka CLI; the recovery therefore moves
    the CLI to the project's Python and rebuilds the extension environment, not
    the other way round, unless the project's Python is below the supported range.
    """
    extension = request.extension_id
    minimum = ".".join(str(part) for part in MINIMUM_PYTHON)
    reinstall = failure_response(
        request,
        code="SANKA_SOURCE_PYTHON_MISMATCH",
        message=(
            f"The project's .venv uses Python {PROJECT_PYTHON_PLACEHOLDER}, but the "
            f"{extension} extension environment was built with Python {required_python}, "
            "the interpreter that runs the sanka CLI. Reinstall the CLI on the project's "
            "Python and rebuild the extension environment: `uv tool install --python "
            f"{PROJECT_PYTHON_PLACEHOLDER} --force sanka-cli`, then `sanka extension remove "
            f"{extension}` and `sanka extension add {extension}`. Alternatively recreate "
            f"the project's .venv with `uv venv --python {required_python} .venv` and "
            "reinstall its requirements."
        ),
    )
    unsupported = failure_response(
        request,
        code="SANKA_SOURCE_PYTHON_MISMATCH",
        message=(
            f"The project's .venv uses Python {PROJECT_PYTHON_PLACEHOLDER}, but {extension} "
            f"requires Python {minimum} or newer. Recreate the project's .venv with "
            f"`uv venv --python {required_python} .venv`, reinstall its requirements, and "
            "keep the sanka CLI on that same Python."
        ),
    )
    return {
        "version_error": json.dumps(encode_response(reinstall)) + "\n",
        "unsupported_python_error": json.dumps(encode_response(unsupported)) + "\n",
    }


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
        sanka_extension_drf_to_fastapi,
        sanka_drf_replay,
        sanka_extensions,
        sanka_extension_sdk,
        sanka_connector,
    ):
        assert package.__file__ is not None
        paths.append(str(Path(package.__file__).resolve().parent.parent))
    required_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    configuration = json.dumps(
        {
            "extension_paths": list(dict.fromkeys(paths)),
            "python_version": list(sys.version_info[:2]),
            "minimum_python": list(MINIMUM_PYTHON),
            **python_mismatch_responses(request, required_python),
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

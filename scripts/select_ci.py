# SPDX-License-Identifier: Apache-2.0
"""Select PR suites using workspace dependencies; unknown changes run everything."""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path


def affected(packages: dict[str, set[str]], paths: list[str]) -> set[str]:
    selected: set[str] = set()
    for path in paths:
        parts = path.split("/")
        # Fixtures are also imported by sibling suites, outside wheel dependencies.
        if (
            len(parts) < 3
            or parts[0] != "packages"
            or parts[1] not in packages
            or "fixtures" in parts
            or parts[-1] == "conftest.py"
        ):
            return set(packages)
        selected.add(parts[1])
    # Include transitive consumers of shared capture, replay and SDK packages.
    while True:
        consumers = {name for name, deps in packages.items() if deps & selected}
        if consumers <= selected:
            return selected
        selected |= consumers


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    projects = {
        path.parent.name: tomllib.loads(path.read_text())["project"]
        for path in (root / "packages").glob("*/pyproject.toml")
    }
    names = {project["name"]: directory for directory, project in projects.items()}
    packages = {
        directory: {
            names[name]
            for dependency in project.get("dependencies", [])
            if (name := re.split(r"[\s<>=!~;\[]", dependency, maxsplit=1)[0]) in names
        }
        for directory, project in projects.items()
    }
    selected = set(packages)
    if os.environ.get("GITHUB_EVENT_NAME") == "pull_request":
        base = os.environ["CI_BASE_SHA"]
        paths = subprocess.check_output(
            ["git", "diff", "--name-only", "--no-renames", f"{base}...HEAD"],
            cwd=root,
            text=True,
        ).splitlines()
        selected = affected(packages, paths)
    outputs = {
        "pytest_args": " ".join(
            f"--ignore=packages/{name}/tests" for name in sorted(set(packages) - selected)
        ),
        "drf": str(
            bool(selected & {"sanka-extension-drf-to-fastapi", "sanka-extension-drf-to-flask"})
        ).lower(),
        "rust": str("sanka-extension-typescript-to-rust" in selected).lower(),
        "mobile": str("sanka-extension-react-native-to-native" in selected).lower(),
        "flask": str("sanka-extension-drf-to-flask" in selected).lower(),
    }
    print("Affected packages:", ", ".join(sorted(selected)))
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
        for name, value in outputs.items():
            stream.write(f"{name}={value}\n")


if __name__ == "__main__":
    main()

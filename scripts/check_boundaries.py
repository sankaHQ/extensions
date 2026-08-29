# SPDX-License-Identifier: Apache-2.0
"""Enforce the SDK/runtime/provider dependency boundaries."""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ROOT / "packages"
SDK_NAME = "sanka-connector-sdk"


def _project(package: Path) -> dict[str, object]:
    with (package / "pyproject.toml").open("rb") as handle:
        document = tomllib.load(handle)
    return document["project"]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def main() -> int:
    errors: list[str] = []
    sdk = PACKAGES / SDK_NAME
    sdk_project = _project(sdk)
    if sdk_project.get("dependencies") != []:
        errors.append("sanka-connector-sdk must have zero runtime dependencies")

    for source in sorted((sdk / "src").rglob("*.py")):
        if not source.read_text(encoding="utf-8").startswith(
            "# SPDX-License-Identifier: Apache-2.0"
        ):
            errors.append(f"missing Apache-2.0 SPDX header: {source.relative_to(ROOT)}")
        for module in _imports(source):
            if module == "sanka" or module.startswith("sanka."):
                errors.append(f"SDK imports the Sanka runtime in {source.relative_to(ROOT)}")

    for package in sorted(PACKAGES.glob("sanka-connector-*")):
        if package.name == SDK_NAME:
            continue
        provider = package.name.removeprefix("sanka-connector-")
        own_module = f"sanka_connector_{provider.replace('-', '_')}"
        project = _project(package)
        dependencies = [str(item).lower() for item in project.get("dependencies", [])]
        if not any(item.startswith(SDK_NAME) for item in dependencies):
            errors.append(f"{package.name} must depend on {SDK_NAME}")

        entry_points = project.get("entry-points")
        connector_entries = (
            entry_points.get("sanka.connectors", {}) if isinstance(entry_points, dict) else {}
        )
        if set(connector_entries) != {provider}:
            errors.append(
                f"{package.name} must own exactly the {provider!r} sanka.connectors entry point"
            )

        for source in sorted((package / "src").rglob("*.py")):
            if not source.read_text(encoding="utf-8").startswith(
                "# SPDX-License-Identifier: Apache-2.0"
            ):
                errors.append(f"missing Apache-2.0 SPDX header: {source.relative_to(ROOT)}")
            for module in _imports(source):
                if module == "sanka" or module.startswith("sanka."):
                    errors.append(
                        f"provider imports the Sanka runtime in {source.relative_to(ROOT)}"
                    )
                if module.startswith("sanka_connector_") and not module.startswith(own_module):
                    errors.append(
                        f"provider imports another provider in {source.relative_to(ROOT)}: {module}"
                    )

    if errors:
        print("Connector boundary validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Connector dependency boundaries: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

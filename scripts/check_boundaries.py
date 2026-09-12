# SPDX-License-Identifier: Apache-2.0
"""Enforce the SDK/runtime/provider dependency boundaries."""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ROOT / "packages"
SDK_NAME = "sanka-connector-sdk"
EXTENSION_SDK_NAME = "sanka-extension-sdk"
EXTENSION_NAMES = ("sanka-extension-drf-to-fastapi", "sanka-extension-drf-to-flask")
FLOW_EXTENSION_NAMES = ("sanka-extension-sales-quote",)
HOSTED_SYSTEM_PROVIDERS = frozenset({"hubspot", "salesforce", "sendgrid"})


def _project(package: Path) -> dict[str, Any]:
    with (package / "pyproject.toml").open("rb") as handle:
        document = tomllib.load(handle)
    return cast(dict[str, Any], document["project"])


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def _is_module_or_submodule(module: str, allowed: tuple[str, ...]) -> bool:
    return any(module == name or module.startswith(f"{name}.") for name in allowed)


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
        if provider in HOSTED_SYSTEM_PROVIDERS:
            errors.append(
                f"{package.name} is hosted-only and must remain in Sanka's managed API runtime"
            )
        own_module = f"sanka_connector_{provider.replace('-', '_')}"
        project = _project(package)
        dependencies = [str(item).lower() for item in project.get("dependencies", [])]
        if not any(item.startswith(EXTENSION_SDK_NAME) for item in dependencies):
            errors.append(f"{package.name} must depend on {EXTENSION_SDK_NAME}")

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

    extension_sdk = PACKAGES / EXTENSION_SDK_NAME
    extension_sdk_project = _project(extension_sdk)
    if extension_sdk_project.get("dependencies") != [f"{SDK_NAME}=={sdk_project['version']}"]:
        errors.append(f"{EXTENSION_SDK_NAME} may depend only on the pinned compatibility SDK")
    extension_version = str(extension_sdk_project["version"])
    for package in (
        extension_sdk,
        *(PACKAGES / name for name in (*EXTENSION_NAMES, *FLOW_EXTENSION_NAMES)),
    ):
        own_module = package.name.replace("-", "_")
        allowed_modules: tuple[str, ...] = (own_module,)
        project = _project(package)
        if package.name in (*EXTENSION_NAMES, *FLOW_EXTENSION_NAMES):
            allowed_modules += ("sanka_extension_sdk", "sanka_extensions")
            expected_dependency = f"{EXTENSION_SDK_NAME}=={extension_version}"
            expected_dependencies = [expected_dependency]
            if package.name in EXTENSION_NAMES:
                expected_dependencies.append("sanka-drf-replay==0.1.0a2")
            if project.get("dependencies") != expected_dependencies:
                errors.append(f"{package.name} must depend exactly on {expected_dependency}")
            if project.get("scripts") != {package.name: f"{own_module}.__main__:main"}:
                errors.append(f"{package.name} must own its exact executable entry point")
        for source in sorted((package / "src").rglob("*.py")):
            if not source.read_text(encoding="utf-8").startswith(
                "# SPDX-License-Identifier: Apache-2.0"
            ):
                errors.append(f"missing Apache-2.0 SPDX header: {source.relative_to(ROOT)}")
            for module in _imports(source):
                if (
                    package.name in FLOW_EXTENSION_NAMES
                    and module.split(".")[0] not in sys.stdlib_module_names
                    and not _is_module_or_submodule(module, allowed_modules)
                ):
                    errors.append(
                        f"Flow generation must use only stdlib and its SDK: "
                        f"{source.relative_to(ROOT)}: {module}"
                    )
                if package.name in FLOW_EXTENSION_NAMES and module.split(".")[0] in {
                    "socket",
                    "urllib",
                    "http",
                    "ftplib",
                    "smtplib",
                    "sqlite3",
                    "subprocess",
                }:
                    errors.append(
                        f"Flow generation cannot call networks, databases or child processes: "
                        f"{source.relative_to(ROOT)}: {module}"
                    )
                if module == "sanka" or module.startswith("sanka."):
                    errors.append(
                        f"extension imports the Sanka runtime in {source.relative_to(ROOT)}"
                    )
                if module.startswith("sanka_extension_") and not _is_module_or_submodule(
                    module, allowed_modules
                ):
                    errors.append(
                        f"extension imports another extension in "
                        f"{source.relative_to(ROOT)}: {module}"
                    )

    replay = PACKAGES / "sanka-drf-replay"
    if _project(replay).get("dependencies") != []:
        errors.append("sanka-drf-replay must have zero runtime dependencies")
    for source in (replay / "src").rglob("*.py"):
        if not source.read_text().startswith("# SPDX-License-Identifier: Apache-2.0"):
            errors.append(f"missing Apache-2.0 SPDX header: {source.relative_to(ROOT)}")
        for module in _imports(source):
            if module.split(".")[0] not in sys.stdlib_module_names:
                errors.append(f"replay must use only stdlib: {source.relative_to(ROOT)}: {module}")

    if errors:
        print("Extension boundary validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Extension dependency boundaries: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

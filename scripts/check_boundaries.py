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
GO_EXTENSION_NAME = "sanka-extension-python-to-golang"
RUST_EXTENSION_NAME = "sanka-extension-typescript-to-rust"
RN_EXTENSION_NAME = "sanka-extension-react-native-to-native"
TS_CAPTURE_NAME = "sanka-ts-capture"
HTTP_REPLAY_NAME = "sanka-http-replay"
JEV_EXTENSION_NAME = "sanka-extension-llm-to-jev"
FLOW_EXTENSION_NAME = "sanka-extension-business-flows"
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
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
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
    # Consumer metadata stays on the independently published SDK until its
    # successor is released; workspace overrides test the candidate separately.
    extension_version = "0.1.0a4"
    for package in (
        extension_sdk,
        *(PACKAGES / name for name in EXTENSION_NAMES),
        PACKAGES / FLOW_EXTENSION_NAME,
        PACKAGES / JEV_EXTENSION_NAME,
        PACKAGES / GO_EXTENSION_NAME,
        PACKAGES / RUST_EXTENSION_NAME,
        PACKAGES / RN_EXTENSION_NAME,
    ):
        own_module = package.name.replace("-", "_")
        allowed_modules: tuple[str, ...] = (own_module,)
        project = _project(package)
        if package.name == JEV_EXTENSION_NAME:
            allowed_modules += ("sanka_extensions",)
            if project.get("dependencies") != ["sanka-extension-sdk==0.1.0a4"]:
                errors.append("Jev converter depends only on the published SDK a4")
            if project.get("scripts") != {package.name: f"{own_module}.__main__:main"}:
                errors.append("Jev converter requires its isolated executable")
        if package.name == GO_EXTENSION_NAME:
            allowed_modules += ("sanka_extension_sdk", "sanka_extensions")
            if project.get("dependencies") != ["sanka-extension-sdk==0.1.0a4"]:
                errors.append("Python to Golang depends only on the published SDK a4")
            if project.get("scripts") != {package.name: f"{own_module}.__main__:main"}:
                errors.append("Python to Golang requires its isolated executable")
        if package.name == RN_EXTENSION_NAME:
            allowed_modules += ("sanka_extension_sdk", "sanka_extensions", "sanka_ts_capture")
            if project.get("dependencies") != [
                "sanka-extension-sdk==0.1.0a4",
                "sanka-ts-capture==0.1.0a1",
            ]:
                errors.append(
                    "React Native to native depends only on the published SDK a4 "
                    "and the TypeScript capture helper"
                )
            if project.get("scripts") != {package.name: f"{own_module}.__main__:main"}:
                errors.append("React Native to native requires its isolated executable")
        if package.name == RUST_EXTENSION_NAME:
            allowed_modules += (
                "sanka_extension_sdk",
                "sanka_extensions",
                "sanka_ts_capture",
                "sanka_http_replay",
            )
            if project.get("dependencies") != [
                "sanka-extension-sdk==0.1.0a4",
                "sanka-ts-capture==0.1.0a1",
                "sanka-http-replay==0.1.0a1",
            ]:
                errors.append(
                    "TypeScript to Rust depends only on the published SDK a4, the TypeScript "
                    "capture helper and the HTTP replay contract"
                )
            if project.get("scripts") != {package.name: f"{own_module}.__main__:main"}:
                errors.append("TypeScript to Rust requires its isolated executable")
        if package.name == FLOW_EXTENSION_NAME:
            if project.get("dependencies") != ["sanka-extension-sdk==0.1.0a5"]:
                errors.append("Business Flow definitions depend only on the published SDK a5")
            if project.get("scripts") != {package.name: f"{own_module}.__main__:main"}:
                errors.append("Business Flow definitions require their isolated executable")
        if package.name in EXTENSION_NAMES:
            allowed_modules += ("sanka_extension_sdk", "sanka_extensions")
            expected_dependency = f"{EXTENSION_SDK_NAME}=={extension_version}"
            if project.get("dependencies") != [
                expected_dependency,
                "sanka-drf-replay==0.1.0a4",
                "sanka-code-migration==0.1.0a3",
            ]:
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
                    package.name == JEV_EXTENSION_NAME
                    and module.split(".")[0] not in sys.stdlib_module_names
                    and not _is_module_or_submodule(module, (own_module, "sanka_extensions"))
                ):
                    errors.append(f"Jev converter imports non-SDK execution code: {module}")
                if (
                    package.name == FLOW_EXTENSION_NAME
                    and module.split(".")[0] not in sys.stdlib_module_names
                    and not _is_module_or_submodule(module, (own_module, "sanka_extensions"))
                ):
                    errors.append(f"Business Flow imports non-SDK execution code: {module}")
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

    helper = PACKAGES / "sanka-code-migration"
    if _project(helper).get("dependencies") != []:
        errors.append("sanka-code-migration must have zero installed runtime dependencies")
    for source in (helper / "src").rglob("*.py"):
        if not source.read_text().startswith("# SPDX-License-Identifier: Apache-2.0"):
            errors.append(f"missing Apache-2.0 SPDX header: {source.relative_to(ROOT)}")
        for module in _imports(source):
            if module == "sanka" or module.startswith(("sanka.", "sanka_extension_")):
                errors.append(
                    f"shared code helper imports runtime or extension: {source}: {module}"
                )

    for helper_name in (TS_CAPTURE_NAME, HTTP_REPLAY_NAME):
        _check_stdlib_helper(PACKAGES / helper_name, helper_name, errors)

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


def _check_stdlib_helper(helper: Path, name: str, errors: list[str]) -> None:
    """Shared helpers ship as standard-library-only wheels with SPDX headers."""
    if _project(helper).get("dependencies") != []:
        errors.append(f"{name} must have zero installed runtime dependencies")
    for source in (helper / "src").rglob("*.py"):
        if not source.read_text().startswith("# SPDX-License-Identifier: Apache-2.0"):
            errors.append(f"missing Apache-2.0 SPDX header: {source.relative_to(ROOT)}")
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            # Relative imports stay inside the helper; absolute ones must be stdlib.
            if isinstance(node, ast.ImportFrom) and node.level:
                continue
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module]
                if isinstance(node, ast.ImportFrom) and node.module
                else []
            )
            for module in names:
                if module.split(".")[0] not in sys.stdlib_module_names:
                    errors.append(
                        f"{name} must use only stdlib: {source.relative_to(ROOT)}: {module}"
                    )


if __name__ == "__main__":
    raise SystemExit(main())

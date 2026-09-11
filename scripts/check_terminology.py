# SPDX-License-Identifier: Apache-2.0
"""Prevent new legacy SDK names outside the explicit compatibility boundary."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY_TYPES = {
    "ConnectorRegistration",
    "SourceConnector",
    "DestinationConnector",
    "ConnectorError",
    "ProviderIdentity",
    "ProviderTimeoutError",
    "TransientProviderError",
}


def check(root: Path) -> list[str]:
    errors = []
    for source in sorted((root / "packages").glob("*/src/**/*.py")):
        # SDK storage and facade are the documented shared compatibility boundary.
        if "sanka-connector-sdk" in source.parts:
            continue
        tree = ast.parse(source.read_text(), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "sanka_connector" or node.module.startswith("sanka_connector."):
                    errors.append(f"{source.relative_to(root)}:{node.lineno}: use sanka_data")
                for alias in node.names:
                    if alias.name in LEGACY_TYPES:
                        errors.append(
                            f"{source.relative_to(root)}:{node.lineno}: "
                            f"legacy SDK type {alias.name}"
                        )
            if isinstance(node, ast.ClassDef) and node.name in LEGACY_TYPES:
                errors.append(f"{source.relative_to(root)}:{node.lineno}: legacy type definition")
    return errors


def main() -> int:
    errors = check(ROOT)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Data-extension terminology: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

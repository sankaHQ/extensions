# SPDX-License-Identifier: Apache-2.0
"""Generate/check the user-visible catalog from exact extension manifests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def catalog_document(root: Path) -> str:
    catalog = json.loads((root / "marketplace.json").read_text())
    lines = [
        "# Extension catalog",
        "",
        "Generated from marketplace.json and extension manifests.",
        "",
        "| Extension | Capability | Supported systems / conversion targets |",
        "| --- | --- | --- |",
    ]
    for item in sorted(catalog["extensions"], key=lambda item: item["id"]):
        manifest = json.loads((root / item["manifest"]).read_text())
        if manifest["kind"] == "connector":
            capability = "Data"
            support = "; ".join(
                f"{system['name']} ({', '.join(system['roles'])})"
                for system in manifest["providers"]
            )
        else:
            capability = "Code"
            support = ", ".join(manifest["targets"])
        lines.append(f"| `{manifest['id']}` | {capability} | {support} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    path = ROOT / "docs/catalog.md"
    expected = catalog_document(ROOT)
    if args.write:
        path.write_text(expected)
    elif not path.is_file() or path.read_text() != expected:
        raise SystemExit(
            "Catalog docs have drifted; run python scripts/check_catalog_docs.py --write"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

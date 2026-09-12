# SPDX-License-Identifier: Apache-2.0
"""Generate/check the user-visible catalog from exact extension manifests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def catalog_document(root: Path) -> str:
    catalog = json.loads((root / "marketplace.json").read_text())
    data_rows = []
    code_rows = []
    for item in sorted(catalog["extensions"], key=lambda item: item["id"]):
        manifest = json.loads((root / item["manifest"]).read_text())
        # Published manifest kinds remain compatibility fields. Catalog sections
        # describe the work, independently of endpoint technology or deployment.
        if manifest["kind"] == "connector":
            for endpoint in manifest["providers"]:
                reads = "Yes" if "source" in endpoint["roles"] else "—"
                writes = "Yes" if "destination" in endpoint["roles"] else "—"
                data_rows.append(
                    f"| `{manifest['id']}` | {endpoint['name']} | {reads} | {writes} |"
                )
        elif manifest["kind"] == "migration":
            targets = ", ".join(manifest["targets"])
            code_rows.append(f"| `{manifest['id']}` | {targets} |")
        else:
            raise ValueError(f"Unsupported extension kind: {manifest['kind']}")
    lines = [
        "# Extension catalog",
        "",
        "Extensions are grouped by what they migrate: **Data**, **Workflow**, or **Code**.",
        "Database engines, file formats and frameworks describe what an extension works with.",
        "Hosting a database yourself or using a managed service does not change its category.",
        "",
        "The available packages and their supported operations are generated from",
        "`marketplace.json` and the extension manifests.",
        "",
        "## Data",
        "",
        "Read or write records and content in configured data endpoints.",
        "An endpoint can be a database, a file or a directory of files.",
        "Readers supply migration sources; writers supply migration destinations.",
        "",
        "| Extension | Data endpoint type | Reads | Writes |",
        "| --- | --- | --- | --- |",
        *data_rows,
        "",
        "## Workflow",
        "",
        "Migrate or reconstruct automations, triggers, actions and conditions.",
        "",
        "There are no executable Workflow extensions in this marketplace yet.",
        "The SDK provides the [Flow definition contract](flow.md); creating a definition",
        "does not construct or activate a workflow.",
        "",
        "## Code",
        "",
        "Convert application code between frameworks or other code technologies.",
        "Each extension defines its supported source projects and conversion targets.",
        "For example, DRF-to-FastAPI converts a Django REST Framework application to FastAPI.",
        "Supported scopes and limitations are documented in each extension's package README.",
        "",
        "| Extension | Conversion target |",
        "| --- | --- |",
        *code_rows,
    ]
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

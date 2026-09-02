# SPDX-License-Identifier: Apache-2.0
"""OPTIONS metadata and 405 parity: captured from DRF at scan time, served natively."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from adapter_cli import run_cli

FIXTURES = Path(__file__).parent / "fixtures"


def _scan_and_generate(tmp_path: Path) -> tuple[Path, dict[str, object], Path]:
    project = tmp_path / "crud"
    shutil.copytree(FIXTURES / "drf_crud_project", project)
    for arguments in (["scan", str(project)], ["plan", str(project), "--to", "fastapi"]):
        completed = run_cli(arguments, project)
        assert completed.returncode == 0, completed.stderr
    plan = json.loads((project / ".sanka" / "plan-fastapi.json").read_text(encoding="utf-8"))
    applied = run_cli(["apply", "--root", str(project), "--plan-hash", plan["plan_hash"]], project)
    assert applied.returncode == 0, applied.stderr
    scan = json.loads((project / ".sanka" / "scan.json").read_text(encoding="utf-8"))
    return project, scan, project / ".sanka" / "output" / "fastapi"


def _route(scan: dict[str, object], method: str, path: str) -> dict[str, object]:
    routes = scan["routes"]
    assert isinstance(routes, list)
    for route in routes:
        if route["method"] == method and route["path"] == path:
            assert isinstance(route, dict)
            return route
    raise AssertionError(f"route not scanned: {method} {path}")


def test_scan_captures_drf_options_metadata_per_path(tmp_path: Path) -> None:
    _project, scan, output = _scan_and_generate(tmp_path)
    listing = _route(scan, "GET", "/api/gadgets/")["options"]
    assert isinstance(listing, dict)
    anonymous = listing["anonymous"]
    assert anonymous["name"] == "Gadget List"
    assert anonymous["description"] == ""
    assert anonymous["renders"] == ["application/json", "text/html"]
    assert anonymous["parses"] == [
        "application/json",
        "application/x-www-form-urlencoded",
        "multipart/form-data",
    ]
    # AllowAny: the anonymous caller already earns the POST field map
    post = anonymous["actions"]["POST"]
    assert post["name"] == {
        "type": "string",
        "required": True,
        "read_only": False,
        "label": "Name",
        "max_length": 80,
    }
    assert listing["authorized"] == anonymous
    detail = _route(scan, "GET", "/api/gadgets/{pk}/")["options"]
    assert isinstance(detail, dict)
    assert detail["anonymous"]["name"] == "Gadget Instance"
    assert "PUT" in detail["anonymous"]["actions"]  # existence is decided at request time
    root = _route(scan, "GET", "/api/")["options"]
    assert isinstance(root, dict)
    assert root["anonymous"]["name"] == "Api Root"
    assert "actions" not in root["anonymous"]
    # the manifest carries the same tables and the generated app mounts OPTIONS + 405
    manifest = json.loads((output / "sanka-manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["options"]) >= {"/api/", "/api/gadgets/", "/api/gadgets/{pk}/"}
    assert manifest["generic_messages"]["method_not_allowed"] == 'Method "{method}" not allowed.'
    app_source = (output / "app.py").read_text(encoding="utf-8")
    assert '@app.options("/api/gadgets/")' in app_source
    # the detail path keeps its lookup converter, exactly like the GET/PUT/PATCH/DELETE routes
    assert '@app.options("/api/gadgets/{pk:' in app_source
    assert "@app.exception_handler(405)" in app_source
    assert "native.method_not_allowed(request)" in app_source

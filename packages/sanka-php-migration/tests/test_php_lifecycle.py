# SPDX-License-Identifier: Apache-2.0
"""Published lifecycle owns review binding, selected scope and artifact safety."""

import dataclasses
import json
from pathlib import Path

import pytest
from sanka_php_migration.lifecycle import handle

from sanka_extensions.code import ExtensionRequest


def request(root: Path, target: str) -> ExtensionRequest:
    extension = "sanka/php-to-" + ("fastapi" if target == "fastapi" else "golang")
    return ExtensionRequest(
        "php-test",
        "plan",
        str(root),
        str(root / ".sanka" / target),
        extension,
        "0.1.0a1",
        "0" * 64,
        {},
        {},
        (),
        None,
    )


def fixture(root: Path) -> None:
    (root / "api.php").write_text("""<?php
function health(array $request) { return ['status'=>200, 'body'=>['ok'=>true]]; }
function blocked(array $request) { return shell_exec('date'); }
""")
    (root / "sanka-php.json").write_text(
        json.dumps(
            {
                "schema": "sanka.php-project/v1",
                "kind": "custom",
                "database": {"dialect": "none"},
                "routes": [
                    {"method": "GET", "path": "/health", "file": "api.php", "handler": "health"},
                    {"method": "GET", "path": "/blocked", "file": "api.php", "handler": "blocked"},
                ],
            }
        )
    )


@pytest.mark.parametrize("target", ["fastapi", "chi"])
def test_selected_closure_review_staleness_and_preserved_repairs(
    tmp_path: Path, target: str
) -> None:
    fixture(tmp_path)
    req = request(tmp_path, target)
    blocked = handle(req, target)
    assert blocked.outcome == "success", blocked.error
    assert blocked.data["blockers"]
    assert blocked.data["files"] == {}
    req = dataclasses.replace(req, configuration={"selected_endpoints": ["GET /health"]})
    planned = handle(req, target)
    assert planned.outcome == "success", planned.error
    assert planned.data["plan_hash"].startswith("sha256:")
    assert len(planned.data["plan_hash"]) == 71
    assert not planned.data["blockers"]
    assert planned.data["capture"]["discovered_routes"] == 2
    assert handle(dataclasses.replace(req, command="apply"), target).outcome == "error"
    approved = dataclasses.replace(
        req,
        command="apply",
        reviewed_plan_hash="runtime-hash",
        configuration=req.configuration | {"extension_plan_hash": planned.data["plan_hash"]},
    )
    before = (tmp_path / "api.php").read_text()
    (tmp_path / "api.php").write_text(before.replace("'ok'", "'changed'"))
    assert handle(approved, target).outcome == "error"
    (tmp_path / "api.php").write_text(before)
    applied = handle(approved, target)
    assert applied.outcome == "success", applied.error
    output = Path(applied.data["output"])
    handler = output / ("handlers.py" if target == "fastapi" else "handlers.go")
    handler.write_text(handler.read_text() + "\n# user repair\n")
    assert handle(approved, target).outcome == "error"
    assert handler.read_text().endswith("# user repair\n")


def test_failed_verify_invalidates_prior_pass_and_rejects_unknown_settings(tmp_path: Path) -> None:
    fixture(tmp_path)
    req = request(tmp_path, "fastapi")
    artifact = Path(req.artifact_root)
    artifact.mkdir(parents=True)
    (artifact / "verify.json").write_text('{"ok":true}')
    failed = handle(dataclasses.replace(req, command="verify"), "fastapi")
    assert failed.outcome == "error"
    assert not (artifact / "verify.json").exists()
    assert (
        handle(dataclasses.replace(req, configuration={"ignore_auth": True}), "fastapi").outcome
        == "error"
    )


def test_artifacts_cannot_follow_symlinks(tmp_path: Path) -> None:
    fixture(tmp_path)
    (tmp_path / "destination").mkdir()
    (tmp_path / ".sanka").symlink_to(tmp_path / "destination")
    assert handle(request(tmp_path, "fastapi"), "fastapi").outcome == "error"
    assert list((tmp_path / "destination").iterdir()) == []

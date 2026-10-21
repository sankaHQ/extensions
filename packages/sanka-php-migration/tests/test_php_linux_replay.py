# SPDX-License-Identifier: Apache-2.0
"""Release gate: real PHP versus native targets, including an edited candidate."""

import dataclasses
import json
import os
import shutil
from pathlib import Path

import pytest
from sanka_php_migration.lifecycle import handle

from sanka_extensions.code import ExtensionRequest

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(os.environ.get("SANKA_PHP_REPLAY") != "1", reason="disposable Linux only"),
]


@pytest.mark.parametrize("target", ["fastapi", "chi"])
@pytest.mark.parametrize(
    "fixture,dialect", [("custom", "none"), ("laravel", "mysql"), ("laravel", "sqlite")]
)
def test_original_source_and_native_candidate_parity(
    tmp_path: Path, target: str, fixture: str, dialect: str
) -> None:
    root = tmp_path / "project"
    shutil.copytree(Path(__file__).parent / "fixtures" / fixture, root)
    if dialect == "sqlite":
        manifest = json.loads((root / "sanka-php.json").read_text())
        manifest["database"]["dialect"] = "sqlite"
        (root / "sanka-php.json").write_text(json.dumps(manifest))
        (root / "schema.sql").write_text(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
            "amount TEXT NOT NULL, owner TEXT NOT NULL);"
        )
        cases = json.loads((root / "sanka-verify.json").read_text())
        for case in cases["scenarios"]:
            if case["id"] == "unchanged-update":
                case["expected_status"] = 200
        (root / "sanka-verify.json").write_text(json.dumps(cases))
    request = ExtensionRequest(
        request_id="php-linux",
        command="scan",
        project_root=str(root),
        artifact_root=str(root / ".sanka" / "php"),
        extension_id="sanka/php-to-" + ("fastapi" if target == "fastapi" else "golang"),
        extension_version="0.1.0a1",
        manifest_digest="sha256:" + "a" * 64,
        fingerprint={},
        configuration={},
        prior_artifacts=(),
        reviewed_plan_hash=None,
    )
    assert handle(request, target).outcome == "success"
    plan = handle(dataclasses.replace(request, command="plan"), target)
    assert plan.outcome == "success", plan.error
    assert not plan.data["blockers"], plan.data["blockers"]
    approved = dataclasses.replace(
        request,
        reviewed_plan_hash="sha256:" + "b" * 64,
        configuration={"extension_plan_hash": plan.data["plan_hash"]},
    )
    assert handle(dataclasses.replace(approved, command="apply"), target).outcome == "success"
    for command in ("test", "verify"):
        result = handle(dataclasses.replace(approved, command=command), target)
        assert result.outcome == "success", result.error
        assert len(result.data["routes"]) == (3 if fixture == "custom" else 5)
    output = root / ".sanka/php" / target
    handlers = output / ("handlers.py" if target == "fastapi" else "handlers.go")
    source = handlers.read_text()
    assert "Unauthorized" in source
    handlers.write_text(source.replace("Unauthorized", "Wrong response"))
    mismatch = handle(dataclasses.replace(approved, command="verify"), target)
    assert mismatch.outcome == "error"
    report = json.loads((root / ".sanka/php/verify.json").read_text())
    assert not report["ok"]
    assert any(route["status"] == "failed" for route in report["routes"])
    # A failed admission must invalidate previous passing or mismatching evidence.
    handlers.unlink()
    assert handle(dataclasses.replace(approved, command="verify"), target).outcome == "error"
    assert not (root / ".sanka/php/verify.json").exists()

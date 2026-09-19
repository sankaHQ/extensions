# SPDX-License-Identifier: Apache-2.0
"""Extraction regressions against captures made before moving the FastAPI scanner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sanka_code_migration.drf.model import FrameworkScan
from sanka_code_migration.hashing import content_hash

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "packages/sanka-extension-drf-to-fastapi/tests/fixtures"
GOLDEN = json.loads(Path(__file__).with_name("drf_capture_golden.json").read_text())


@pytest.mark.parametrize("fixture", sorted(GOLDEN))
def test_existing_source_capture_is_unchanged(fixture: str, tmp_path: Path) -> None:
    script = """
import json, sys
from sanka_code_migration.drf.scan import scan_django
scan = scan_django(sys.argv[1], artifact_dir=sys.argv[2])
print(json.dumps(scan.to_dict()))
"""
    env = dict(os.environ)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    env.pop("SANKA_TEST_DB", None)
    result = subprocess.run(
        [sys.executable, "-c", script, str(FIXTURES / fixture), str(tmp_path)],
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert {key: content_hash(payload[key]) for key in GOLDEN[fixture]} == GOLDEN[fixture]
    assert FrameworkScan.from_dict(payload).with_hash().scan_hash == payload["scan_hash"]


def test_fastapi_compatibility_imports_keep_identity() -> None:
    from sanka_code_migration.drf import access_contracts, metadata, nested_create, parity, scan

    from sanka_extension_drf_to_fastapi import django_fastapi, model
    from sanka_extension_drf_to_fastapi.access_contracts import UnsupportedContract
    from sanka_extension_drf_to_fastapi.metadata import route_options_metadata
    from sanka_extension_drf_to_fastapi.nested_create import lower_nested_create
    from sanka_extension_drf_to_fastapi.parity import route_parity_notes

    assert model.FrameworkScan is FrameworkScan
    assert django_fastapi.scan_django is scan.scan_django
    assert django_fastapi.FrameworkMigrationError is scan.FrameworkMigrationError
    assert UnsupportedContract is access_contracts.UnsupportedContract
    assert route_options_metadata is metadata.route_options_metadata
    assert lower_nested_create is nested_create.lower_nested_create
    assert route_parity_notes is parity.route_parity_notes

# SPDX-License-Identifier: Apache-2.0
"""Regression tests for marketplace wheel closure and immutable hashes."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.update_marketplace_hashes import MANIFEST_WHEELS, update_manifests


def _wheel(directory: Path, name: str) -> None:
    (directory / name).write_bytes(name.encode())


def test_hash_updater_records_each_manifest_dependency_closure(tmp_path: Path) -> None:
    for names in MANIFEST_WHEELS.values():
        for name in names:
            _wheel(tmp_path, name)

    manifests = update_manifests(tmp_path, release_tag="extensions-v0.1.0a11")

    assert set(manifests) == set(MANIFEST_WHEELS)
    for package, payload in manifests.items():
        assert [wheel["name"] for wheel in payload["wheels"]] == list(MANIFEST_WHEELS[package])
        assert all(
            wheel["url"].startswith(
                "https://github.com/sankaHQ/extensions/releases/download/extensions-v0.1.0a11/"
            )
            and len(wheel["sha256"]) == 64
            for wheel in payload["wheels"]
        )


def test_hash_updater_rejects_an_incomplete_or_wrongly_tagged_wheel_set(tmp_path: Path) -> None:
    _wheel(tmp_path, "sanka_connector_sdk-0.1.0a11-py3-none-any.whl")

    with pytest.raises(RuntimeError, match="complete marketplace wheel set"):
        update_manifests(tmp_path, release_tag="extensions-v0.1.0a11")
    with pytest.raises(RuntimeError, match=r"extensions-v0\.1\.0a11"):
        update_manifests(tmp_path, release_tag="extensions-v0.1.0a12")

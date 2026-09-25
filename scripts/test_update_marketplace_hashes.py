# SPDX-License-Identifier: Apache-2.0
"""A code-only marketplace must not rewrite published manifest URLs."""

from pathlib import Path

import pytest

from scripts.update_marketplace_hashes import MANIFEST_WHEELS, RELEASE_TAG, update_manifests


def test_code_only_release_leaves_published_manifests_unchanged(tmp_path: Path) -> None:
    for name in {name for closure in MANIFEST_WHEELS.values() for name in closure}:
        (tmp_path / name).write_bytes(b"pinned")
    assert update_manifests(tmp_path, release_tag=RELEASE_TAG) == {}


def test_incomplete_release_cannot_update_manifests(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="complete marketplace wheel set"):
        update_manifests(tmp_path, release_tag=RELEASE_TAG)

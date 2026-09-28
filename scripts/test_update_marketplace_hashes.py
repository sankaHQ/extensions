# SPDX-License-Identifier: Apache-2.0
"""Incomplete bundles must not produce publishable manifest updates."""

from pathlib import Path

import pytest

from scripts.update_marketplace_hashes import RELEASE_TAG, update_manifests


def test_incomplete_release_cannot_update_manifests(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="complete marketplace wheel set"):
        update_manifests(tmp_path, release_tag=RELEASE_TAG)

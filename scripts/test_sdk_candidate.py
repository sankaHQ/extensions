# SPDX-License-Identifier: Apache-2.0
"""SDK source publication must not replace the marketplace's pinned old wheel."""

from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.build_release import PINNED_EXTENSION_SDK
from scripts.check_sdk_candidate import validate


def test_published_sdk_identity_is_separate_from_candidate() -> None:
    assert PINNED_EXTENSION_SDK.name == "sanka_extension_sdk-0.1.0a4-py3-none-any.whl"
    assert "/sdk-v0.1.0a4/" in PINNED_EXTENSION_SDK.url
    assert (
        PINNED_EXTENSION_SDK.sha256
        == "f1a6655095ab81e549137e1d9604492bda2a31677d674c6359b0a39a2da96307"
    )


def test_candidate_cannot_reuse_published_version(tmp_path: Path) -> None:
    with (
        patch(
            "scripts.check_sdk_candidate.tomllib.loads",
            return_value={"project": {"version": "0.1.0a4"}},
        ),
        pytest.raises(ValueError, match="reuse"),
    ):
        validate(tmp_path)


def test_candidate_rejects_missing_wheel(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly"):
        validate(tmp_path)

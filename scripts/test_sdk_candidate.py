# SPDX-License-Identifier: Apache-2.0
"""SDK source publication must not replace the marketplace's pinned old wheel."""

from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import build_release
from scripts.build_release import PINNED_EXTENSION_SDK, LockedWheel
from scripts.check_sdk_candidate import validate


def test_marketplace_build_fetches_pinned_sdk_instead_of_rebuilding_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[str] = []
    downloaded: list[LockedWheel] = []

    def run(command: list[str], **kwargs: object) -> None:
        built.append(command[command.index("--package") + 1])

    def download(directory: Path, wheel: LockedWheel) -> Path:
        downloaded.append(wheel)
        return directory / wheel.name

    monkeypatch.setattr(build_release, "_prepare_output", lambda path: path)
    monkeypatch.setattr(build_release.subprocess, "run", run)
    monkeypatch.setattr(build_release, "download_locked_wheel", download)
    build_release.build(tmp_path)
    assert "sanka-extension-sdk" not in built
    assert set(built) == set(build_release.MARKETPLACE_PACKAGES) - {"sanka-extension-sdk"}
    assert downloaded.count(PINNED_EXTENSION_SDK) == 1


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

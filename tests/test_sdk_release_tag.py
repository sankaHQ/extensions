# SPDX-License-Identifier: Apache-2.0
"""Publication refuses to attach reviewed wheels to a different source revision."""

from __future__ import annotations

import runpy
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

release = SimpleNamespace(
    **runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/ensure_sdk_release_tag.py"))
)

SHA = "a" * 40
REF = "refs/tags/" + release.TAG


@pytest.mark.parametrize(
    "existing", ["", f"{SHA}\t{REF}\n", f"{'b' * 40}\t{REF}\n{SHA}\t{REF}^{{}}\n"]
)
def test_missing_or_matching_tag_is_bound_to_reviewed_source(monkeypatch, existing):
    reads = iter([existing, f"{SHA}\t{REF}\n"])
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 0, next(reads) if "ls-remote" in command else ""
        )

    monkeypatch.setattr(release.subprocess, "run", run)
    release.ensure_tag(SHA)
    pushes = [call for call in calls if "push" in call]
    assert pushes == ([] if existing else [["git", "push", "origin", f"{SHA}:{REF}"]])


@pytest.mark.parametrize("before", [False, True])
def test_wrong_source_before_or_after_creation_fails_without_force(monkeypatch, before):
    reads = iter([f"{'b' * 40}\t{REF}\n"] if before else ["", f"{'b' * 40}\t{REF}\n"])
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 0, next(reads) if "ls-remote" in command else ""
        )

    monkeypatch.setattr(release.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="refusing publication"):
        release.ensure_tag(SHA)
    assert all("--force" not in call for call in calls)
    assert not before or all("push" not in call for call in calls)

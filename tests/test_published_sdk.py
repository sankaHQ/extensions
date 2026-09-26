# SPDX-License-Identifier: Apache-2.0
"""Extension-only releases must not move SDK tags or replace SDK wheel bytes."""

from __future__ import annotations

import hashlib
import io
import runpy
from pathlib import Path

import pytest

MODULE = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/verify_published_sdk.py")
)
VERIFY = MODULE["verify_published_sdk"]
GLOBALS = VERIFY.__globals__


@pytest.mark.parametrize("failure", [None, "tag", "local", "published", "missing"])
def test_sdk_reuse_requires_original_source_and_both_wheel_copies(tmp_path, monkeypatch, failure):
    original = b"reviewed SDK wheel"
    expected = hashlib.sha256(original).hexdigest()
    monkeypatch.setitem(GLOBALS, "SDK_WHEELS", {"sdk.whl": expected})
    monkeypatch.setattr(
        MODULE["ensure_sdk_release_tag"],
        "remote_revision",
        lambda: "wrong" if failure == "tag" else MODULE["SDK_REVISION"],
    )
    (tmp_path / "sdk.whl").write_bytes(b"changed" if failure == "local" else original)
    downloads = []

    def download(url, *, timeout):
        downloads.append(url)
        assert timeout == 30
        if failure == "missing":
            raise OSError("SDK asset is not published")
        return io.BytesIO(b"changed" if failure == "published" else original)

    monkeypatch.setitem(GLOBALS, "urlopen", download)
    if failure:
        with pytest.raises((RuntimeError, OSError)):
            VERIFY(tmp_path)
    else:
        VERIFY(tmp_path)
        assert downloads == [
            "https://github.com/sankaHQ/extensions/releases/download/sdk-v0.1.0a4/sdk.whl"
        ]
    if failure in {"tag", "local"}:
        assert downloads == []

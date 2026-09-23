# SPDX-License-Identifier: Apache-2.0
"""Extension-only releases must not move SDK tags or replace SDK wheel bytes."""

from __future__ import annotations

import hashlib
import io
import runpy
from pathlib import Path

import pytest

from scripts import build_release

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


def test_candidate_builds_only_new_versions_and_reuses_published_wheels() -> None:
    assert build_release.MARKETPLACE_PACKAGES == ()
    assert {wheel.name for wheel in build_release.PINNED_LOCAL_WHEELS} == {
        "sanka_extension_sdk-0.1.0a4-py3-none-any.whl",
        "sanka_drf_replay-0.1.0a4-py3-none-any.whl",
        "sanka_code_migration-0.1.0a3-py3-none-any.whl",
        "sanka_extension_drf_to_fastapi-0.1.0a17-py3-none-any.whl",
        "sanka_extension_drf_to_flask-0.1.0a12-py3-none-any.whl",
        "sanka_connector_sdk-0.1.0a12-py3-none-any.whl",
        "sanka_connector_markdown-0.1.0a14-py3-none-any.whl",
        "sanka_connector_csv-0.1.0a14-py3-none-any.whl",
        "sanka_connector_sqlite-0.1.0a14-py3-none-any.whl",
        "sanka_connector_postgres-0.1.0a14-py3-none-any.whl",
        "sanka_connector_clickhouse-0.1.0a14-py3-none-any.whl",
    }

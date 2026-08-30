# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
from pathlib import Path

import pytest

from sanka_connector import (
    ConfigurationError,
    Credentials,
    DataError,
    SourceFilter,
    UnsupportedFeatureError,
)
from sanka_connector_markdown import CONNECTOR, MarkdownSource


def _credentials(root: Path) -> Credentials:
    return Credentials(provider="markdown", settings={"connection": str(root)})


def _write_content(root: Path) -> None:
    (root / "a.md").write_text(
        "---\ntitle: A\npublished: true\nviews: 10\n---\nAlpha body\n", encoding="utf-8"
    )
    (root / "b.md").write_text(
        "---\ntitle: B\nviews: many\ntags:\n  - x\n  - y\n---\nBeta body\n", encoding="utf-8"
    )
    (root / "c.md").write_text("Plain body, no frontmatter\n", encoding="utf-8")


async def test_inventory_reports_fields_counts_and_warnings(tmp_path: Path) -> None:
    _write_content(tmp_path)
    (tmp_path / "broken.md").write_text("---\ntitle: [unclosed\n---\nbody\n", encoding="utf-8")

    inventory = await MarkdownSource().inventory(_credentials(tmp_path))

    assert len(inventory.objects) == 1
    documents = inventory.objects[0]
    assert documents.record_count == 4
    assert documents.identity_fields == ["path"]
    field_keys = [f.key for f in documents.fields]
    assert field_keys[:3] == ["path", "slug", "content"]
    assert {"title", "published", "views", "tags"} <= set(field_keys)
    assert any("mixed types" in w and "views" in w for w in inventory.warnings)
    assert any("unparseable frontmatter" in w for w in inventory.warnings)


async def test_read_records_paginates_deterministically(tmp_path: Path) -> None:
    _write_content(tmp_path)
    source = MarkdownSource()
    credentials = _credentials(tmp_path)

    first = await source.read_records(
        credentials, object_type="documents", field_keys=["path", "title", "content"], limit=2
    )
    assert [r["path"] for r in first.records] == ["a.md", "b.md"]
    assert first.has_more and first.next_cursor == "2"

    second = await source.read_records(
        credentials,
        object_type="documents",
        field_keys=["path", "title", "content"],
        limit=2,
        cursor=first.next_cursor,
    )
    assert [r["path"] for r in second.records] == ["c.md"]
    assert not second.has_more and second.next_cursor is None
    assert second.records[0]["title"] is None
    assert "Plain body" in second.records[0]["content"]


async def test_count_capability_and_registration(tmp_path: Path) -> None:
    _write_content(tmp_path)
    count = await MarkdownSource().count_records(_credentials(tmp_path), object_type="documents")
    assert count == 3
    assert CONNECTOR.name == "markdown"
    assert CONNECTOR.source is not None and CONNECTOR.destination is None


async def test_source_filter_is_rejected_before_directory_access(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    source_filter = SourceFilter(field="published")
    with pytest.raises(UnsupportedFeatureError):
        await MarkdownSource().read_records(
            _credentials(missing),
            object_type="documents",
            field_keys=["path"],
            limit=1,
            source_filter=source_filter,
        )
    with pytest.raises(UnsupportedFeatureError):
        await MarkdownSource().count_records(
            _credentials(missing), object_type="documents", source_filter=source_filter
        )


async def test_count_rejects_unknown_object_type(tmp_path: Path) -> None:
    _write_content(tmp_path)
    with pytest.raises(DataError):
        await MarkdownSource().count_records(_credentials(tmp_path), object_type="other")


async def test_external_markdown_symlink_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")
    (root / "leak.md").symlink_to(outside)

    with pytest.raises(DataError, match="symbolic-link"):
        await MarkdownSource().inventory(_credentials(root))


async def test_markdown_replacement_race_cannot_escape_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        pytest.skip("descriptor-relative no-follow opens are unavailable")

    root = tmp_path / "root"
    root.mkdir()
    victim = root / "victim.md"
    victim.write_text("safe\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")
    real_open = os.open
    replaced = False

    def racing_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        if path == "victim.md" and dir_fd is not None and not replaced:
            victim.unlink()
            victim.symlink_to(outside)
            replaced = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises(DataError, match="cannot be opened safely"):
        await MarkdownSource().inventory(_credentials(root))
    assert replaced


async def test_symlinked_configured_root_preserves_relative_paths(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "nested").mkdir()
    (actual / "nested" / "safe.md").write_text("safe\n", encoding="utf-8")
    configured = tmp_path / "configured"
    configured.symlink_to(actual, target_is_directory=True)

    page = await MarkdownSource().read_records(
        _credentials(configured), object_type="documents", field_keys=["path"], limit=10
    )
    assert page.records == [{"path": "nested/safe.md"}]


async def test_missing_directory_is_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        await MarkdownSource().inventory(_credentials(tmp_path / "nope"))

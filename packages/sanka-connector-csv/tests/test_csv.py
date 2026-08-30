# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from sanka_connector import (
    ConfigurationError,
    Credentials,
    DataError,
    SourceFilter,
    SupportsRecordCounts,
    UnsupportedFeatureError,
)
from sanka_connector_csv import CONNECTOR, CsvSource, _open_regular_text


def _credentials(path: Path) -> Credentials:
    return Credentials(provider="csv", settings={"connection": str(path)})


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


async def test_delimiter_sniffing_csv_and_tsv(tmp_path: Path) -> None:
    semicolons = _write(tmp_path / "orders.csv", "id;total\n1;9.99\n2;12.50\n")
    orders = (await CsvSource().inventory(_credentials(semicolons))).objects[0]
    assert orders.key == "orders"
    assert [f.key for f in orders.fields] == ["id", "total"]
    assert orders.record_count == 2

    tabs = _write(tmp_path / "Items List.tsv", "sku\tqty\nA-1\t3\nB-2\t5\n")
    items = (await CsvSource().inventory(_credentials(tabs))).objects[0]
    assert items.key == "items_list"
    assert items.label == "Items List"
    assert [f.key for f in items.fields] == ["row", "sku", "qty"]
    assert items.record_count == 2


async def test_single_column_files_fall_back_to_default_delimiters(tmp_path: Path) -> None:
    plain = _write(tmp_path / "names.csv", "name\nalpha\nbeta\n")
    page = await CsvSource().read_records(
        _credentials(plain), object_type="names", field_keys=["name"], limit=10
    )
    assert [r["name"] for r in page.records] == ["alpha", "beta"]

    tsv = _write(tmp_path / "names.tsv", "name\ngamma\n")
    page = await CsvSource().read_records(
        _credentials(tsv), object_type="names", field_keys=["name"], limit=10
    )
    assert [r["name"] for r in page.records] == ["gamma"]


async def test_inventory_with_id_column_infers_types(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "products.csv",
        "ID,name,price,active,mixed\n1,Widget,9.99,true,1\n2,Gadget,12,false,x\n3,Gizmo,,TRUE,\n",
    )
    inventory = await CsvSource().inventory(_credentials(path))
    products = inventory.objects[0]
    assert products.key == "products"
    assert products.record_count == 3
    assert products.identity_fields == ["ID"]
    assert {f.key: f.data_type for f in products.fields} == {
        "ID": "number",
        "name": "string",
        "price": "number",
        "active": "boolean",
        "mixed": "string",
    }
    assert any("mixed" in w and "mixed types" in w for w in inventory.warnings)
    assert not any("synthesized" in w for w in inventory.warnings)


async def test_inventory_without_id_synthesizes_row_identity(tmp_path: Path) -> None:
    path = _write(tmp_path / "notes.csv", "title,body\nA,alpha\nB,beta\n")
    inventory = await CsvSource().inventory(_credentials(path))
    notes = inventory.objects[0]
    assert notes.identity_fields == ["row"]
    assert notes.fields[0].key == "row"
    assert notes.fields[0].data_type == "number"
    assert any("synthesized" in w for w in inventory.warnings)


async def test_read_records_paginates_deterministically(tmp_path: Path) -> None:
    path = _write(tmp_path / "notes.csv", "title,body\nA,alpha\nB,beta\nC,gamma\n")
    source = CsvSource()
    credentials = _credentials(path)

    first = await source.read_records(
        credentials, object_type="notes", field_keys=["row", "title", "missing"], limit=2
    )
    assert first.records == [
        {"row": 1, "title": "A", "missing": None},
        {"row": 2, "title": "B", "missing": None},
    ]
    assert first.has_more and first.next_cursor == "2"

    second = await source.read_records(
        credentials,
        object_type="notes",
        field_keys=["row", "title", "missing"],
        limit=2,
        cursor=first.next_cursor,
    )
    assert second.records == [{"row": 3, "title": "C", "missing": None}]
    assert not second.has_more and second.next_cursor is None

    with pytest.raises(DataError):
        await source.read_records(credentials, object_type="other", field_keys=["title"], limit=1)


async def test_record_values_stay_strings(tmp_path: Path) -> None:
    path = _write(tmp_path / "products.csv", "id,price,active\n1,9.99,true\n")
    page = await CsvSource().read_records(
        _credentials(path), object_type="products", field_keys=["id", "price", "active"], limit=1
    )
    assert page.records == [{"id": "1", "price": "9.99", "active": "true"}]


async def test_count_capability(tmp_path: Path) -> None:
    path = _write(tmp_path / "notes.csv", "title\nA\nB\nC\n")
    source = CsvSource()
    assert await source.count_records(_credentials(path), object_type="notes") == 3
    assert isinstance(source, SupportsRecordCounts)


async def test_source_filter_is_rejected_before_file_access(tmp_path: Path) -> None:
    missing = tmp_path / "missing.csv"
    source_filter = SourceFilter(field="active")
    with pytest.raises(UnsupportedFeatureError):
        await CsvSource().read_records(
            _credentials(missing),
            object_type="missing",
            field_keys=["id"],
            limit=1,
            source_filter=source_filter,
        )
    with pytest.raises(UnsupportedFeatureError):
        await CsvSource().count_records(
            _credentials(missing), object_type="missing", source_filter=source_filter
        )


async def test_missing_and_empty_files_are_configuration_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        await CsvSource().inventory(_credentials(tmp_path / "absent.csv"))
    empty = _write(tmp_path / "empty.csv", "")
    with pytest.raises(ConfigurationError):
        await CsvSource().inventory(_credentials(empty))


async def test_duplicate_canonical_headers_fail_before_rows_are_built(tmp_path: Path) -> None:
    duplicate = _write(tmp_path / "duplicate.csv", " id ,ID,value\n1,2,x\n")
    with pytest.raises(ConfigurationError, match="duplicate canonical"):
        await CsvSource().inventory(_credentials(duplicate))


async def test_synthetic_row_header_collision_fails_closed(tmp_path: Path) -> None:
    collision = _write(tmp_path / "collision.csv", "row,value\n1,x\n")
    with pytest.raises(ConfigurationError, match="synthesized identity"):
        await CsvSource().inventory(_credentials(collision))


async def test_symlinked_csv_is_rejected(tmp_path: Path) -> None:
    outside = _write(tmp_path / "outside.csv", "id,value\n1,secret\n")
    link = tmp_path / "source.csv"
    link.symlink_to(outside)
    with pytest.raises(DataError, match="symbolic link"):
        await CsvSource().inventory(_credentials(link))


async def test_csv_source_limits_are_enforced(tmp_path: Path) -> None:
    path = _write(tmp_path / "limited.csv", "id,value\n1,a\n2,b\n")
    row_limited = Credentials(provider="csv", settings={"connection": str(path), "max_rows": 1})
    with pytest.raises(DataError, match="max_rows=1"):
        await CsvSource().count_records(row_limited, object_type="limited")

    byte_limited = Credentials(provider="csv", settings={"connection": str(path), "max_bytes": 4})
    with pytest.raises(DataError, match="max_bytes=4"):
        await CsvSource().inventory(byte_limited)


def test_csv_parent_replacement_cannot_redirect_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "source"
    parent.mkdir()
    path = _write(parent / "records.csv", "id,value\n1,original\n")
    moved = tmp_path / "moved"
    real_open = os.open
    swapped = False

    def swap_before_file_open(
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if target == "records.csv" and dir_fd is not None and not swapped:
            swapped = True
            parent.rename(moved)
            parent.mkdir()
            _write(parent / "records.csv", "id,value\n1,replacement\n")
        return real_open(target, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("sanka_connector_csv.os.open", swap_before_file_open)
    with _open_regular_text(path, max_bytes=1024) as stream:
        assert "original" in stream.read()


def test_csv_growth_after_open_is_still_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path / "growing.csv", "id\n1\n")
    real_read = os.read
    grown = False

    def grow_then_read(descriptor: int, size: int) -> bytes:
        nonlocal grown
        if not grown:
            grown = True
            with path.open("ab") as stream:
                stream.write(b"x" * 100)
        return real_read(descriptor, size)

    monkeypatch.setattr("sanka_connector_csv.os.read", grow_then_read)
    with (
        pytest.raises(DataError, match="max_bytes=8"),
        _open_regular_text(path, max_bytes=8) as stream,
    ):
        stream.read()


def test_csv_reader_streams_without_materializing_full_file(tmp_path: Path) -> None:
    path = _write(tmp_path / "streamed.csv", "id,value\n1,first\n2,second\n")

    with _open_regular_text(path, max_bytes=1024) as stream:
        assert not isinstance(stream, io.StringIO)
        assert stream.readline() == "id,value\n"


def test_registration_shape() -> None:
    assert CONNECTOR.name == "csv"
    assert CONNECTOR.source is not None
    assert CONNECTOR.destination is None

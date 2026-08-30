# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import sqlite3
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
from sanka_connector_sqlite import CONNECTOR, SqliteSource


def _credentials(path: Path) -> Credentials:
    return Credentials(provider="sqlite", settings={"connection": str(path)})


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO paths require POSIX")
async def test_fifo_source_is_rejected_without_blocking(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    os.mkfifo(database)

    with pytest.raises(DataError, match="not a private regular file"):
        await SqliteSource().inventory(_credentials(database))


def _seed(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL,
            in_stock BOOLEAN,
            notes
        );
        CREATE TABLE logs (message TEXT);
        CREATE TABLE pairs (a TEXT, b TEXT, PRIMARY KEY (a, b)) WITHOUT ROWID;
        """
    )
    connection.executemany(
        "INSERT INTO items (id, name, price, in_stock, notes) VALUES (?, ?, ?, ?, ?)",
        [(index, f"item-{index}", 1.5 * index, index % 2, None) for index in range(1, 6)],
    )
    connection.executemany("INSERT INTO logs (message) VALUES (?)", [("m1",), ("m2",), ("m3",)])
    connection.execute("INSERT INTO pairs (a, b) VALUES ('x', 'y')")
    connection.commit()
    connection.close()


async def test_discover_objects_lists_user_tables_only(tmp_path: Path) -> None:
    db = tmp_path / "in.db"
    _seed(db)
    objects = await SqliteSource().discover_objects(_credentials(db))
    # AUTOINCREMENT creates sqlite_sequence, which must be skipped as an internal.
    assert [o.key for o in objects] == ["items", "logs", "pairs"]
    assert all(o.default_selected and o.canonical_type == o.key for o in objects)


async def test_inventory_primary_key_table(tmp_path: Path) -> None:
    db = tmp_path / "in.db"
    _seed(db)
    inventory = await SqliteSource().inventory(_credentials(db), object_types=["items"])
    assert len(inventory.objects) == 1
    items = inventory.objects[0]
    assert items.record_count == 5
    assert items.identity_fields == ["id"]
    assert {f.key: f.data_type for f in items.fields} == {
        "id": "number",
        "name": "string",
        "price": "number",
        "in_stock": "boolean",
        "notes": "string",
    }


async def test_inventory_rowid_and_without_rowid_paths(tmp_path: Path) -> None:
    db = tmp_path / "in.db"
    _seed(db)
    inventory = await SqliteSource().inventory(_credentials(db))
    by_key = {o.key: o for o in inventory.objects}

    logs = by_key["logs"]
    assert logs.identity_fields == ["rowid"]
    assert [f.key for f in logs.fields] == ["message", "rowid"]
    assert logs.fields[-1].data_type == "number"

    pairs = by_key["pairs"]
    assert pairs.identity_fields == []
    assert any("pairs" in w for w in inventory.warnings)


async def test_read_records_keyset_paginates_across_pages(tmp_path: Path) -> None:
    db = tmp_path / "in.db"
    _seed(db)
    source = SqliteSource()
    credentials = _credentials(db)

    first = await source.read_records(
        credentials, object_type="items", field_keys=["id", "name"], limit=2
    )
    assert [r["id"] for r in first.records] == [1, 2]
    assert first.has_more and first.next_cursor == "2"

    second = await source.read_records(
        credentials, object_type="items", field_keys=["id", "name"], limit=2, cursor="2"
    )
    assert [r["id"] for r in second.records] == [3, 4]
    assert second.has_more and second.next_cursor == "4"

    third = await source.read_records(
        credentials, object_type="items", field_keys=["id", "name"], limit=2, cursor="4"
    )
    assert [r["id"] for r in third.records] == [5]
    assert third.records[0]["name"] == "item-5"
    assert not third.has_more and third.next_cursor is None


async def test_read_records_rowid_path_and_missing_fields(tmp_path: Path) -> None:
    db = tmp_path / "in.db"
    _seed(db)
    source = SqliteSource()
    credentials = _credentials(db)

    first = await source.read_records(
        credentials, object_type="logs", field_keys=["message", "rowid", "absent"], limit=2
    )
    assert first.records == [
        {"message": "m1", "rowid": 1, "absent": None},
        {"message": "m2", "rowid": 2, "absent": None},
    ]
    assert first.has_more and first.next_cursor == "2"

    rest = await source.read_records(
        credentials,
        object_type="logs",
        field_keys=["message", "rowid", "absent"],
        limit=2,
        cursor=first.next_cursor,
    )
    assert rest.records == [{"message": "m3", "rowid": 3, "absent": None}]
    assert not rest.has_more and rest.next_cursor is None

    # The cursor advances even when the identity is not among the requested fields.
    unrequested = await source.read_records(
        credentials, object_type="logs", field_keys=["message"], limit=1
    )
    assert unrequested.records == [{"message": "m1"}]
    assert unrequested.has_more and unrequested.next_cursor == "1"


async def test_without_rowid_composite_key_reads_are_unsupported(tmp_path: Path) -> None:
    db = tmp_path / "in.db"
    _seed(db)
    with pytest.raises(UnsupportedFeatureError):
        await SqliteSource().read_records(
            _credentials(db), object_type="pairs", field_keys=["a", "b"], limit=10
        )


async def test_count_capability_and_registration_has_both_roles(tmp_path: Path) -> None:
    db = tmp_path / "in.db"
    _seed(db)
    source = SqliteSource()
    assert await source.count_records(_credentials(db), object_type="items") == 5
    assert await source.count_records(_credentials(db), object_type="logs") == 3
    assert isinstance(source, SupportsRecordCounts)
    assert CONNECTOR.name == "sqlite"
    assert CONNECTOR.source is not None
    assert CONNECTOR.destination is not None


async def test_source_filter_is_rejected_before_database_access(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    source_filter = SourceFilter(field="active")
    with pytest.raises(UnsupportedFeatureError):
        await SqliteSource().read_records(
            _credentials(missing),
            object_type="items",
            field_keys=["id"],
            limit=1,
            source_filter=source_filter,
        )
    with pytest.raises(UnsupportedFeatureError):
        await SqliteSource().count_records(
            _credentials(missing), object_type="items", source_filter=source_filter
        )


async def test_missing_database_is_configuration_error(tmp_path: Path) -> None:
    missing = tmp_path / "absent.db"
    with pytest.raises(ConfigurationError):
        await SqliteSource().discover_objects(_credentials(missing))
    # Reading must never create an empty database file as a side effect.
    assert not missing.exists()


async def test_declared_rowid_does_not_masquerade_as_hidden_identity(tmp_path: Path) -> None:
    db = tmp_path / "shadow.db"
    connection = sqlite3.connect(db)
    connection.execute('CREATE TABLE logs ("rowid" TEXT, message TEXT)')
    connection.executemany(
        'INSERT INTO logs ("rowid", message) VALUES (?, ?)',
        [("same", "first"), ("same", "second")],
    )
    connection.commit()
    connection.close()

    inventory = await SqliteSource().inventory(_credentials(db))
    assert inventory.objects[0].identity_fields == []
    with pytest.raises(UnsupportedFeatureError, match="keyset pagination"):
        await SqliteSource().read_records(
            _credentials(db), object_type="logs", field_keys=["rowid", "message"], limit=1
        )


async def test_symlinked_sqlite_source_is_rejected(tmp_path: Path) -> None:
    actual = tmp_path / "actual.db"
    _seed(actual)
    linked = tmp_path / "linked.db"
    linked.symlink_to(actual)
    with pytest.raises(DataError, match="symbolic link"):
        await SqliteSource().inventory(_credentials(linked))


async def test_symlinked_sqlite_source_sidecar_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "in.db"
    _seed(database)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("sentinel", encoding="utf-8")
    Path(f"{database}-wal").symlink_to(sentinel)

    with pytest.raises(DataError, match=r"sidecar.*symbolic link"):
        await SqliteSource().inventory(_credentials(database))

    assert sentinel.read_text(encoding="utf-8") == "sentinel"


async def test_source_parent_replacement_cannot_redirect_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "source"
    parent.mkdir()
    database = parent / "source.db"
    _seed(database)
    moved = tmp_path / "moved"
    real_open = os.open
    swapped = False

    def swap_before_database_open(
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if target == "source.db" and dir_fd is not None and not swapped:
            swapped = True
            parent.rename(moved)
            parent.mkdir()
            replacement = sqlite3.connect(parent / "source.db")
            replacement.execute("CREATE TABLE replacement (id INTEGER PRIMARY KEY)")
            replacement.commit()
            replacement.close()
        return real_open(target, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("sanka_connector_sqlite.os.open", swap_before_database_open)
    objects = await SqliteSource().discover_objects(_credentials(database))
    assert [item.key for item in objects] == ["items", "logs", "pairs"]


async def test_cached_source_uses_private_snapshot_after_original_changes(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    _seed(database)
    source = SqliteSource()
    credentials = _credentials(database)
    first = await source.count_records(credentials, object_type="items")

    replacement = sqlite3.connect(database)
    replacement.execute("INSERT INTO items (name, price, in_stock) VALUES ('later', 1, 1)")
    replacement.commit()
    replacement.close()

    assert first == 5
    assert await source.count_records(credentials, object_type="items") == 5

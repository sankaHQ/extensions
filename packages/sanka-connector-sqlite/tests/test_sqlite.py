# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import sanka_connector_sqlite as sqlite_connector
from sanka_connector import Credentials, DataError, WriteOptions
from sanka_connector_sqlite import CONNECTOR, SqliteDestination, _identifier


def _credentials(path: Path) -> Credentials:
    return Credentials(provider="sqlite", settings={"connection": str(path)})


OPTIONS = WriteOptions(conflict_policy="update_existing", identity_fields=["path"])


async def test_create_update_skip_and_schema_evolution(tmp_path: Path) -> None:
    db = tmp_path / "out.db"
    destination = SqliteDestination()
    credentials = _credentials(db)

    created = await destination.write_record(
        credentials,
        object_type="documents",
        properties={"path": "a.md", "title": "A", "published": True, "tags": ["x", "y"]},
        options=OPTIONS,
    )
    assert created.status == "created"

    updated = await destination.write_record(
        credentials,
        object_type="documents",
        properties={"path": "a.md", "title": "A2", "views": 10},
        options=OPTIONS,
    )
    assert updated.status == "updated"
    assert updated.destination_record_id == created.destination_record_id

    skipped = await destination.write_record(
        credentials,
        object_type="documents",
        properties={"path": "a.md", "title": "ignored"},
        options=WriteOptions(conflict_policy="skip_existing", identity_fields=["path"]),
    )
    assert skipped.status == "skipped"

    rows = (
        sqlite3.connect(db)
        .execute("SELECT path, title, published, tags, views FROM documents")
        .fetchall()
    )
    assert rows == [("a.md", "A2", 1, '["x", "y"]', 10)]


async def test_inventory_readback_and_target_suggestion(tmp_path: Path) -> None:
    db = tmp_path / "out.db"
    destination = SqliteDestination()
    credentials = _credentials(db)
    for index in range(3):
        await destination.write_record(
            credentials,
            object_type="documents",
            properties={"path": f"{index}.md"},
            options=OPTIONS,
        )

    inventory = await destination.inventory(credentials, canonical_types={"documents", "absent"})
    assert len(inventory.objects) == 1
    assert inventory.objects[0].record_count == 3
    lossy = destination.automatic_target_object("My Docs!")
    assert lossy is not None and lossy.startswith("sanka_e_my_docs_")
    assert lossy != destination.automatic_target_object("my docs")
    assert destination.automatic_target_object("my_docs") == "my_docs"
    assert CONNECTOR.name == "sqlite"
    assert CONNECTOR.destination is not None and CONNECTOR.source is not None


async def test_destination_rejects_incomplete_composite_identity(tmp_path: Path) -> None:
    destination = SqliteDestination()
    with pytest.raises(DataError, match="missing required identity"):
        await destination.write_record(
            _credentials(tmp_path / "out.db"),
            object_type="documents",
            properties={"tenant": "acme", "title": "missing external id"},
            options=WriteOptions(
                conflict_policy="update_existing",
                identity_fields=["tenant", "external_id"],
            ),
        )
    assert not (tmp_path / "out.db").exists()


async def test_destination_rejects_empty_record_with_declared_identity(tmp_path: Path) -> None:
    with pytest.raises(DataError, match="missing required identity"):
        await SqliteDestination().write_record(
            _credentials(tmp_path / "out.db"),
            object_type="documents",
            properties={},
            options=OPTIONS,
        )
    assert not (tmp_path / "out.db").exists()


async def test_symlinked_sqlite_destination_is_rejected(tmp_path: Path) -> None:
    actual = tmp_path / "actual.db"
    sqlite3.connect(actual).close()
    linked = tmp_path / "linked.db"
    linked.symlink_to(actual)
    with pytest.raises(DataError, match="symbolic link"):
        await SqliteDestination().write_record(
            _credentials(linked),
            object_type="documents",
            properties={"path": "a.md"},
            options=OPTIONS,
        )


async def test_symlinked_sqlite_destination_parent_is_rejected(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)

    with pytest.raises(DataError, match="symbolic link"):
        await SqliteDestination().write_record(
            _credentials(linked / "nested" / "out.db"),
            object_type="documents",
            properties={"path": "a.md"},
            options=OPTIONS,
        )

    assert not (actual / "nested").exists()


async def test_symlinked_sqlite_destination_sidecar_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "out.db"
    sqlite3.connect(database).close()
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("sentinel", encoding="utf-8")
    Path(f"{database}-wal").symlink_to(sentinel)

    with pytest.raises(DataError, match=r"sidecar.*symbolic link"):
        await SqliteDestination().write_record(
            _credentials(database),
            object_type="documents",
            properties={"path": "a.md"},
            options=OPTIONS,
        )

    assert sentinel.read_text(encoding="utf-8") == "sentinel"


async def test_inventory_on_missing_database_is_empty_and_creates_nothing(
    tmp_path: Path,
) -> None:
    db = tmp_path / "never-created.db"
    inventory = await SqliteDestination().inventory(_credentials(db), canonical_types={"documents"})
    assert inventory.objects == []
    assert not db.exists()  # planning/inspection must leave no artifacts


async def test_sqlite_url_prefix_is_accepted(tmp_path: Path) -> None:
    db = tmp_path / "url.db"
    destination = SqliteDestination()
    result = await destination.write_record(
        Credentials(provider="sqlite", settings={"connection": f"sqlite:///{db}"}),
        object_type="items",
        properties={"path": "x", "n": 1},
        options=OPTIONS,
    )
    assert result.status == "created"
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


async def test_declared_rowid_identity_updates_without_hidden_locator_confusion(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rowid.db"
    destination = SqliteDestination()
    credentials = _credentials(database)
    options = WriteOptions(conflict_policy="update_existing", identity_fields=["rowid"])

    created = await destination.write_record(
        credentials,
        object_type="records",
        properties={"rowid": "shadow", "value": "first"},
        options=options,
    )
    updated = await destination.write_record(
        credentials,
        object_type="records",
        properties={"rowid": "shadow", "value": "second"},
        options=options,
    )

    rows = sqlite3.connect(database).execute('SELECT "rowid", value FROM records').fetchall()
    assert rows == [("shadow", "second")]
    assert updated.destination_record_id == created.destination_record_id


def test_identifier_mapping_is_collision_resistant() -> None:
    assert _identifier("a-b", kind="column") != _identifier("a b", kind="column")
    assert _identifier("A", kind="column") != _identifier("a", kind="column")


async def test_missing_destination_parent_replacement_cannot_redirect_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination_parent = tmp_path / "destination"
    destination_parent.mkdir()
    moved_parent = tmp_path / "moved-destination"
    database = destination_parent / "out.db"
    real_open_parent = sqlite_connector._open_or_create_destination_parent

    def replacing_open_parent(path: Path) -> int:
        descriptor = real_open_parent(path)
        destination_parent.rename(moved_parent)
        destination_parent.mkdir()
        return descriptor

    monkeypatch.setattr(
        sqlite_connector, "_open_or_create_destination_parent", replacing_open_parent
    )
    with pytest.raises(DataError, match="parent changed before publication"):
        await SqliteDestination().write_record(
            _credentials(database),
            object_type="documents",
            properties={"path": "a.md"},
            options=OPTIONS,
        )

    assert not database.exists()
    assert not (moved_parent / "out.db").exists()


async def test_existing_destination_sandwich_is_rejected_before_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "out.db"
    original = sqlite3.connect(database)
    original.execute("CREATE TABLE original (value TEXT)")
    original.commit()
    original.close()
    replacement = tmp_path / "replacement.db"
    attacker = sqlite3.connect(replacement)
    attacker.execute("CREATE TABLE attacker (value TEXT)")
    attacker.commit()
    attacker.close()
    saved = tmp_path / "saved.db"
    real_snapshot = sqlite_connector._snapshot_database_at

    def sandwich_snapshot(
        directory_fd: int,
        database_name: str,
        *,
        display_path: Path,
        expected_identity: tuple[int, int] | None,
        role: str,
    ) -> Path:
        database.rename(saved)
        replacement.rename(database)
        try:
            return real_snapshot(
                directory_fd,
                database_name,
                display_path=display_path,
                expected_identity=expected_identity,
                role=role,
            )
        finally:
            database.rename(replacement)
            saved.rename(database)

    monkeypatch.setattr(sqlite_connector, "_snapshot_database_at", sandwich_snapshot)
    with pytest.raises(DataError, match="changed before snapshot"):
        await SqliteDestination().write_record(
            _credentials(database),
            object_type="documents",
            properties={"path": "a.md"},
            options=OPTIONS,
        )

    table = (
        sqlite3.connect(database)
        .execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        .fetchone()
    )
    assert table == ("original",)


async def test_sidecar_created_after_private_session_is_rejected_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "out.db"
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("sentinel", encoding="utf-8")
    real_prepare = sqlite_connector._prepare_destination_session

    def injecting_prepare(path: Path) -> sqlite_connector._DestinationSession:
        session = real_prepare(path)
        Path(f"{path}-journal").symlink_to(sentinel)
        return session

    monkeypatch.setattr(sqlite_connector, "_prepare_destination_session", injecting_prepare)
    with pytest.raises(DataError, match=r"sidecar.*symbolic link"):
        await SqliteDestination().write_record(
            _credentials(database),
            object_type="documents",
            properties={"path": "a.md"},
            options=OPTIONS,
        )

    assert sentinel.read_text(encoding="utf-8") == "sentinel"
    assert not database.exists()

# SPDX-License-Identifier: Apache-2.0
"""SQLite source and destination connector.

As a source, every user table is a migratable object: fields come from
``PRAGMA table_info``, the identity is the table's single-column primary key
(else ``rowid``, exposed as an extra field), and reads use keyset pagination
(``WHERE <pk> > ? ORDER BY <pk>``) so cursors are deterministic and resumable.

As a destination, tables are created lazily from the records written to them
(TEXT-affinity columns; complex values JSON-encoded), and widened with
``ALTER TABLE`` when new fields appear. Writes honor the identity fields and
conflict policy from :class:`sanka_connector.WriteOptions`.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from sanka_connector import (
    ConfigurationError,
    ConnectorRegistration,
    Credentials,
    DataError,
    FieldSchema,
    Inventory,
    ObjectSchema,
    RecordPage,
    RelationshipWrite,
    RelationshipWriteResult,
    SourceFilter,
    SourceObject,
    UnsupportedFeatureError,
    WriteOptions,
    WriteResult,
    require_identity_values,
)

_IDENTIFIER = re.compile(r"[^a-z0-9_]+")
_ROWID = "rowid"
_RESERVED_IDENTIFIER_PREFIX = "sanka_"
_ENCODED_IDENTIFIER_PREFIX = "sanka_e_"
_ESCAPED_IDENTIFIER_PREFIX = "sanka_r_"
_MAX_SOURCE_DATABASE_BYTES = 16 * 1024 * 1024 * 1024


class _DestinationSession:
    def __init__(
        self,
        *,
        destination: Path,
        working: Path,
        parent_identity: tuple[int, int],
        published_identity: tuple[int, int] | None,
    ) -> None:
        self.destination = destination
        self.working = working
        self.parent_identity = parent_identity
        self.published_identity = published_identity


def _identifier(value: str, *, kind: str) -> str:
    raw = value
    normalized = _IDENTIFIER.sub("_", raw.strip().lower()).strip("_")
    if not normalized:
        raise DataError(f"cannot derive a SQLite {kind} name from {value!r}")
    if normalized[0].isdigit():
        normalized = f"t_{normalized}"
    if raw == normalized and not normalized.startswith(_RESERVED_IDENTIFIER_PREFIX):
        return normalized
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    prefix = (
        _ESCAPED_IDENTIFIER_PREFIX
        if raw == normalized and normalized.startswith(_RESERVED_IDENTIFIER_PREFIX)
        else _ENCODED_IDENTIFIER_PREFIX
    )
    return f"{prefix}{normalized}_{digest}"


def _normalized_properties(properties: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    origins: dict[str, str] = {}
    for raw_name, value in properties.items():
        name = _identifier(raw_name, kind="column")
        if name in normalized:
            raise DataError(
                f"SQLite fields {origins[name]!r} and {raw_name!r} map to "
                f"the same destination column {name!r}"
            )
        normalized[name] = _to_sql(value)
        origins[name] = raw_name
    return normalized


def _reject_filter(source_filter: SourceFilter | None) -> None:
    if source_filter is not None:
        raise UnsupportedFeatureError(
            "sqlite source filters are not supported",
            remediation="remove the source filter or use a connector that supports it",
        )


def _quote(name: str) -> str:
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _database_path(credentials: Credentials, *, role: str) -> str:
    raw = credentials.settings.get("connection") or credentials.settings.get("path")
    if not raw:
        raise ConfigurationError(f"sqlite {role} needs a database path (connection)")
    path_text = str(raw)
    for prefix in ("sqlite:///", "sqlite://"):
        if path_text.startswith(prefix):
            path_text = path_text[len(prefix) :]
            break
    if not path_text:
        raise ConfigurationError(f"sqlite {role} path is empty")
    return str(Path(path_text).expanduser())


def _column_type(declared: str) -> str:
    upper = declared.upper()
    if "BOOL" in upper:
        return "boolean"
    if "INT" in upper or any(marker in upper for marker in ("REAL", "FLOA", "DOUB")):
        return "number"
    return "string"


class _SqliteConnectionCache:
    """Per-instance connection cache shared by the source and destination roles."""

    def __init__(self) -> None:
        self._connections: dict[tuple[str, bool], sqlite3.Connection] = {}
        self._destination_sessions: dict[str, _DestinationSession] = {}

    def _cached_connection(self, key: str, *, read_only: bool) -> sqlite3.Connection:
        cache_key = (key, read_only)
        path = Path(key).absolute()
        connection = self._connections.get(cache_key)
        if connection is None:
            if read_only:
                snapshot = _snapshot_read_only_database(path)
                connection = sqlite3.connect(f"{snapshot.as_uri()}?mode=ro&immutable=1", uri=True)
            else:
                session = _prepare_destination_session(path)
                connection = sqlite3.connect(session.working)
                connection.execute("PRAGMA journal_mode=DELETE").fetchone()
                self._destination_sessions[key] = session
            self._connections[cache_key] = connection
        elif not read_only:
            if key not in self._destination_sessions:
                raise DataError(f"sqlite destination connection has no private session: {path}")
        return connection

    def _commit_destination(self, key: str, connection: sqlite3.Connection) -> None:
        session = self._destination_sessions.get(key)
        if session is None:
            raise DataError("sqlite destination connection has no private session")
        connection.commit()
        session.published_identity = _publish_destination_session(session)


class SqliteSource(_SqliteConnectionCache):
    provider = "sqlite"
    binding_kind = "database"

    async def discover_objects(self, credentials: Credentials) -> list[SourceObject]:
        connection = self._connect(credentials)
        return [
            SourceObject(key=table, label=table, canonical_type=table, default_selected=True)
            for table in self._tables(connection)
        ]

    async def inventory(
        self,
        credentials: Credentials,
        *,
        object_types: list[str] | None = None,
    ) -> Inventory:
        connection = self._connect(credentials)
        tables = self._tables(connection)
        if object_types is not None:
            requested = set(object_types)
            tables = [table for table in tables if table in requested]

        objects: list[ObjectSchema] = []
        warnings: list[str] = []
        for table in tables:
            columns = self._columns(connection, table)
            fields = [
                FieldSchema(
                    key=str(column[1]),
                    label=str(column[1]),
                    data_type=_column_type(str(column[2])),
                )
                for column in columns
            ]
            identity = self._identity_column(connection, table, columns)
            identity_fields: list[str] = []
            if identity == _ROWID:
                fields.append(FieldSchema(key=_ROWID, label=_ROWID, data_type="number"))
                identity_fields = [_ROWID]
            elif identity is not None:
                identity_fields = [identity]
            else:
                warnings.append(
                    f"table {table!r} has neither a single-column primary key nor a rowid;"
                    " no identity field is available"
                )
            count = connection.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0]
            objects.append(
                ObjectSchema(
                    key=table,
                    label=table,
                    canonical_type=table,
                    record_count=int(count),
                    fields=fields,
                    identity_fields=identity_fields,
                )
            )
        return Inventory(
            provider=self.provider,
            connection_id=credentials.connection_id,
            objects=objects,
            warnings=warnings,
        )

    async def read_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        field_keys: list[str],
        limit: int,
        cursor: str | None = None,
        source_filter: SourceFilter | None = None,
    ) -> RecordPage:
        _reject_filter(source_filter)
        connection = self._connect(credentials)
        self._require_table(connection, object_type)
        columns = self._columns(connection, object_type)
        declared = {str(column[1]) for column in columns}
        key_column = self._identity_column(connection, object_type, columns)
        if key_column is None:
            raise UnsupportedFeatureError(
                f"table {object_type!r} has neither a single-column primary key nor a rowid;"
                " keyset pagination is unavailable",
                remediation="add a single-column primary key to the table",
            )

        select_columns = [key for key in field_keys if key in declared or key == key_column]
        if key_column not in select_columns:
            select_columns.append(key_column)
        rendered = ", ".join(
            f"{_ROWID} AS {_ROWID}" if name == _ROWID and name not in declared else _quote(name)
            for name in select_columns
        )
        key_reference = (
            _ROWID if key_column == _ROWID and key_column not in declared else _quote(key_column)
        )

        page_size = max(1, limit)
        sql = f"SELECT {rendered} FROM {_quote(object_type)}"
        parameters: list[Any] = []
        if cursor is not None:
            sql += f" WHERE {key_reference} > ?"
            parameters.append(cursor)
        sql += f" ORDER BY {key_reference} LIMIT ?"
        parameters.append(page_size + 1)

        rows = connection.execute(sql, parameters).fetchall()
        has_more = len(rows) > page_size
        records: list[dict[str, Any]] = []
        last_key: Any = None
        for row in rows[:page_size]:
            values = dict(zip(select_columns, row, strict=True))
            last_key = values[key_column]
            if last_key is None:
                raise DataError(
                    f"table {object_type!r} has a NULL value in identity column "
                    f"{key_column!r}; keyset pagination requires non-NULL keys"
                )
            records.append({key: values.get(key) for key in field_keys})
        return RecordPage(
            object_key=object_type,
            records=records,
            next_cursor=str(last_key) if has_more else None,
            has_more=has_more,
        )

    async def count_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
    ) -> int:
        _reject_filter(source_filter)
        connection = self._connect(credentials)
        self._require_table(connection, object_type)
        row = connection.execute(f"SELECT COUNT(*) FROM {_quote(object_type)}").fetchone()
        return int(row[0])

    # -- internals ----------------------------------------------------------

    def _connect(self, credentials: Credentials) -> sqlite3.Connection:
        key = _database_path(credentials, role="source")
        return self._cached_connection(key, read_only=True)

    def _tables(self, connection: sqlite3.Connection) -> list[str]:
        rows = connection.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            " ORDER BY name"
        ).fetchall()
        return [str(row[0]) for row in rows]

    def _require_table(self, connection: sqlite3.Connection, object_type: str) -> None:
        if object_type not in self._tables(connection):
            raise DataError(f"sqlite source has no table {object_type!r}")

    def _columns(self, connection: sqlite3.Connection, table: str) -> list[Any]:
        return connection.execute(f"PRAGMA table_info({_quote(table)})").fetchall()

    def _identity_column(
        self, connection: sqlite3.Connection, table: str, columns: list[Any]
    ) -> str | None:
        primary_key = [str(column[1]) for column in columns if int(column[5]) > 0]
        if len(primary_key) == 1:
            return primary_key[0]
        if _ROWID in {str(column[1]).casefold() for column in columns}:
            return None
        if self._has_rowid(connection, table):
            return _ROWID
        return None

    def _has_rowid(self, connection: sqlite3.Connection, table: str) -> bool:
        try:
            connection.execute(f"SELECT {_ROWID} FROM {_quote(table)} LIMIT 1")
        except sqlite3.OperationalError:
            return False
        return True


class SqliteDestination(_SqliteConnectionCache):
    provider = "sqlite"
    binding_kind = "database"

    def automatic_target_object(self, canonical_type: str) -> str | None:
        return _identifier(canonical_type, kind="table")

    async def inventory(
        self,
        credentials: Credentials,
        *,
        canonical_types: set[str],
    ) -> Inventory:
        # Inventory is a read: a database that does not exist yet is simply
        # empty — never create the file from an inspection/planning path.
        destination_path = Path(_database_path(credentials, role="destination")).absolute()
        if not destination_path.exists() and not destination_path.is_symlink():
            return Inventory(provider=self.provider, connection_id=credentials.connection_id)
        connection = self._connect(credentials, read_only=True)
        objects: list[ObjectSchema] = []
        for canonical_type in sorted(canonical_types):
            table = _identifier(canonical_type, kind="table")
            if not self._table_exists(connection, table):
                continue
            count = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            objects.append(
                ObjectSchema(
                    key=table,
                    label=table,
                    canonical_type=canonical_type,
                    record_count=int(count),
                    fields=[
                        FieldSchema(key=str(column[1]), label=str(column[1])) for column in columns
                    ],
                )
            )
        return Inventory(
            provider=self.provider, connection_id=credentials.connection_id, objects=objects
        )

    async def write_record(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        properties: dict[str, Any],
        options: WriteOptions,
    ) -> WriteResult:
        identity_values = require_identity_values(properties, options.identity_fields)
        if not properties:
            return WriteResult(status="skipped", message="empty record")
        columns = _normalized_properties(properties)
        key = _database_path(credentials, role="destination")
        connection = self._connect(credentials)
        table = _identifier(object_type, kind="table")
        self._ensure_table(connection, table, columns.keys())

        identity_columns = [_identifier(field, kind="column") for field, _ in identity_values]
        exists = self._find_existing(connection, table, identity_columns, columns)
        identity_record_id = _identity_record_id(identity_columns, columns)

        if exists and options.conflict_policy == "skip_existing":
            self._commit_destination(key, connection)
            return WriteResult(status="skipped", destination_record_id=identity_record_id)
        if exists and options.conflict_policy == "update_existing":
            assignments = ", ".join(f'"{name}" = ?' for name in columns)
            predicate = " AND ".join(f'"{name}" = ?' for name in identity_columns)
            connection.execute(
                f'UPDATE "{table}" SET {assignments} WHERE {predicate}',
                (*columns.values(), *(columns[name] for name in identity_columns)),
            )
            self._commit_destination(key, connection)
            return WriteResult(status="updated", destination_record_id=identity_record_id)

        names = ", ".join(f'"{name}"' for name in columns)
        placeholders = ", ".join("?" for _ in columns)
        inserted = connection.execute(
            f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})',
            tuple(columns.values()),
        )
        self._commit_destination(key, connection)
        return WriteResult(
            status="created",
            destination_record_id=identity_record_id or str(inserted.lastrowid),
        )

    async def write_relationship(
        self,
        credentials: Credentials,
        *,
        relationship: RelationshipWrite,
    ) -> RelationshipWriteResult:
        return RelationshipWriteResult(
            status="skipped", message="sqlite destination does not model relationships yet"
        )

    # -- internals ----------------------------------------------------------

    def _connect(self, credentials: Credentials, *, read_only: bool = False) -> sqlite3.Connection:
        key = _database_path(credentials, role="destination")
        return self._cached_connection(key, read_only=read_only)

    def _table_exists(self, connection: sqlite3.Connection, table: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        return row is not None

    def _ensure_table(self, connection: sqlite3.Connection, table: str, columns: Any) -> None:
        column_list = list(columns)
        if not self._table_exists(connection, table):
            rendered = ", ".join(f'"{name}"' for name in column_list)
            connection.execute(f'CREATE TABLE "{table}" ({rendered})')
            return
        existing = {
            str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
        for name in column_list:
            if name not in existing:
                connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}"')

    def _find_existing(
        self,
        connection: sqlite3.Connection,
        table: str,
        identity_columns: list[str],
        columns: dict[str, Any],
    ) -> bool:
        if not identity_columns:
            return False
        predicate = " AND ".join(f'"{name}" = ?' for name in identity_columns)
        row = connection.execute(
            f'SELECT 1 FROM "{table}" WHERE {predicate} LIMIT 1',
            tuple(columns[name] for name in identity_columns),
        ).fetchone()
        return row is not None


def _identity_record_id(identity_columns: list[str], columns: dict[str, Any]) -> str | None:
    if not identity_columns:
        return None
    payload = json.dumps(
        [[name, columns[name]] for name in identity_columns],
        ensure_ascii=False,
        sort_keys=False,
        separators=(",", ":"),
    )
    return "identity:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _to_sql(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if value is None or isinstance(value, int | float | str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _open_destination_parent(path: Path) -> int:
    absolute = path.absolute()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(absolute.anchor, flags)
    try:
        for component in absolute.parent.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_or_create_destination_parent(path: Path) -> int:
    """Create missing parent components without dropping the trusted descriptor chain."""

    absolute = path.absolute()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(absolute.anchor, flags)
    current_component: str | None = None
    try:
        for component in absolute.parent.parts[1:]:
            current_component = component
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                with suppress(FileExistsError):
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError as error:
        if current_component is not None:
            try:
                failed_info = os.stat(current_component, dir_fd=descriptor, follow_symlinks=False)
            except OSError:
                failed_info = None
            if failed_info is not None and stat.S_ISLNK(failed_info.st_mode):
                os.close(descriptor)
                raise DataError(
                    f"sqlite database path contains a symbolic link: {absolute.parent}"
                ) from error
        os.close(descriptor)
        raise DataError(
            f"sqlite destination parent cannot be opened safely: {absolute.parent}"
        ) from error


def _destination_identity_at(directory_fd: int, name: str) -> tuple[int, int] | None:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise DataError(f"sqlite destination is a symbolic link: {name}")
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise DataError(f"sqlite destination is not a private regular file: {name}")
    return (info.st_dev, info.st_ino)


def _prepare_destination_session(path: Path) -> _DestinationSession:
    parent_fd = _open_or_create_destination_parent(path)
    try:
        parent_info = os.fstat(parent_fd)
        parent_identity = (parent_info.st_dev, parent_info.st_ino)
        published_identity = _destination_identity_at(parent_fd, path.name)
        _reject_active_sidecars_at(parent_fd, path.name, role="destination")
        if published_identity is None:
            temporary = Path(tempfile.mkdtemp(prefix="sanka-sqlite-destination-"))
            atexit.register(shutil.rmtree, temporary, ignore_errors=True)
            working = temporary / "destination.db"
        else:
            working = _snapshot_database_at(
                parent_fd,
                path.name,
                display_path=path,
                expected_identity=published_identity,
                role="destination",
            )
        if _destination_identity_at(parent_fd, path.name) != published_identity:
            raise DataError(f"sqlite destination changed while preparing: {path}")
        _reject_active_sidecars_at(parent_fd, path.name, role="destination")
        return _DestinationSession(
            destination=path,
            working=working,
            parent_identity=parent_identity,
            published_identity=published_identity,
        )
    finally:
        os.close(parent_fd)


def _publish_destination_session(session: _DestinationSession) -> tuple[int, int]:
    parent_fd = _open_destination_parent(session.destination)
    temporary_name = f".{session.destination.name}.sanka-{secrets.token_hex(12)}.tmp"
    source_fd: int | None = None
    destination_fd: int | None = None
    working_parent_fd: int | None = None
    try:
        parent_info = os.fstat(parent_fd)
        if (parent_info.st_dev, parent_info.st_ino) != session.parent_identity:
            raise DataError(
                "sqlite destination parent changed before publication: "
                f"{session.destination.parent}"
            )
        if (
            _destination_identity_at(parent_fd, session.destination.name)
            != session.published_identity
        ):
            raise DataError(f"sqlite destination changed before publication: {session.destination}")
        _reject_active_sidecars_at(parent_fd, session.destination.name, role="destination")
        working_parent_fd = os.open(
            session.working.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        _reject_active_sidecars_at(working_parent_fd, session.working.name, role="destination")
        source_fd = os.open(
            session.working.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=working_parent_fd,
        )
        source_info = os.fstat(source_fd)
        if not stat.S_ISREG(source_info.st_mode) or source_info.st_nlink != 1:
            raise DataError("sqlite private working database is not a regular file")
        if source_info.st_size > _MAX_SOURCE_DATABASE_BYTES:
            raise DataError("sqlite destination exceeds the secure publication size limit")
        destination_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        total = 0
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_SOURCE_DATABASE_BYTES:
                raise DataError("sqlite destination grew beyond the publication size limit")
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise OSError("could not publish the SQLite destination")
                view = view[written:]
        os.fsync(destination_fd)
        os.close(destination_fd)
        destination_fd = None
        os.replace(
            temporary_name,
            session.destination.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
        identity = _destination_identity_at(parent_fd, session.destination.name)
        if identity is None:
            raise DataError("sqlite destination disappeared after publication")
        return identity
    except OSError as error:
        raise DataError(
            f"sqlite destination could not be published safely: {session.destination}"
        ) from error
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if working_parent_fd is not None:
            os.close(working_parent_fd)
        if destination_fd is not None:
            os.close(destination_fd)
        with suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=parent_fd)
        os.close(parent_fd)


def _snapshot_read_only_database(path: Path) -> Path:
    """Copy a stable no-follow database into private storage before SQLite opens it."""

    absolute = path.absolute()
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fds: list[int] = []
    try:
        parent_fd = os.open(absolute.anchor, directory_flags)
        directory_fds.append(parent_fd)
        for component in absolute.parent.parts[1:]:
            parent_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            directory_fds.append(parent_fd)
        return _snapshot_database_at(
            parent_fd,
            absolute.name,
            display_path=path,
            expected_identity=None,
            role="source",
        )
    except FileNotFoundError as error:
        raise ConfigurationError(f"sqlite database path does not exist: {path}") from error
    except OSError as error:
        if any(component.is_symlink() for component in (absolute, *absolute.parents)):
            raise DataError(f"sqlite database path contains a symbolic link: {path}") from error
        raise DataError(f"sqlite database path cannot be opened safely: {path}") from error
    finally:
        for descriptor in reversed(directory_fds):
            os.close(descriptor)


def _snapshot_database_at(
    directory_fd: int,
    database_name: str,
    *,
    display_path: Path,
    expected_identity: tuple[int, int] | None,
    role: str,
) -> Path:
    """Snapshot one database through a retained parent descriptor."""

    source_fd: int | None = None
    destination_fd: int | None = None
    try:
        _reject_active_sidecars_at(directory_fd, database_name, role=role)
        source_fd = os.open(
            database_name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory_fd,
        )
        opened = os.fstat(source_fd)
        opened_identity = (opened.st_dev, opened.st_ino)
        if expected_identity is not None and opened_identity != expected_identity:
            raise DataError(f"sqlite {role} database changed before snapshot: {display_path}")
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise DataError(
                f"sqlite {role} database path is not a private regular file: {display_path}"
            )
        if opened.st_size > _MAX_SOURCE_DATABASE_BYTES:
            raise DataError(
                f"sqlite {role} database exceeds the secure snapshot size limit "
                f"({_MAX_SOURCE_DATABASE_BYTES} bytes): {display_path}"
            )
        temporary = Path(tempfile.mkdtemp(prefix=f"sanka-sqlite-{role}-"))
        atexit.register(shutil.rmtree, temporary, ignore_errors=True)
        snapshot = temporary / "database.db"
        destination_fd = os.open(
            snapshot,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        total = 0
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_SOURCE_DATABASE_BYTES:
                raise DataError(
                    f"sqlite {role} database grew beyond the snapshot limit: {display_path}"
                )
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise OSError("could not copy the SQLite database")
                view = view[written:]
        final = os.fstat(source_fd)
        if (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise DataError(f"sqlite {role} database changed while snapshotting: {display_path}")
        _reject_active_sidecars_at(directory_fd, database_name, role=role)
        os.fsync(destination_fd)
        os.close(destination_fd)
        destination_fd = None
        return snapshot
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)


def _reject_active_sidecars_at(directory_fd: int, database_name: str, *, role: str) -> None:
    for suffix in ("-journal", "-wal", "-shm"):
        name = database_name + suffix
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise DataError(f"sqlite database sidecar is a symbolic link: {name}")
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DataError(f"sqlite database sidecar is not a private regular file: {name}")
        raise DataError(
            f"sqlite {role} database has an active sidecar {name!r}; checkpoint and close "
            f"the {role} database before migration"
        )


CONNECTOR = ConnectorRegistration(
    name="sqlite", source=SqliteSource(), destination=SqliteDestination()
)

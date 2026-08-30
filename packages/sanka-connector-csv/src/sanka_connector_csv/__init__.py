# SPDX-License-Identifier: Apache-2.0
"""CSV file source connector.

One ``.csv`` (or ``.tsv``) file is one migratable object: the sanitized file
stem is the object key, the header row defines the fields, and every data row
becomes one record. Rows are read in file order so pagination cursors are
deterministic. Values are passed through as the strings the file contains —
number/boolean inference informs the inventory schema only. A column named
``id`` (case-insensitive) is the identity; otherwise a synthetic ``row``
field (the 1-based data-row index) is exposed and used, with a warning.
"""

from __future__ import annotations

import csv
import io
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from sanka_connector import (
    ConfigurationError,
    ConnectorRegistration,
    Credentials,
    DataError,
    FieldSchema,
    Inventory,
    ObjectSchema,
    RecordPage,
    SourceFilter,
    SourceObject,
    UnsupportedFeatureError,
)

_ROW_FIELD = "row"
_KEY_SANITIZER = re.compile(r"[^a-z0-9_]+")
_SNIFF_DELIMITERS = ",\t;|"
_SNIFF_SAMPLE_CHARS = 8192
_DEFAULT_MAX_BYTES = 64 * 1024 * 1024
_DEFAULT_MAX_ROWS = 1_000_000


class _BoundedDescriptorReader(io.RawIOBase):
    """Streaming no-follow reader with a hard byte ceiling and stability check."""

    def __init__(self, descriptor: int, *, path: Path, max_bytes: int) -> None:
        super().__init__()
        self._descriptor = descriptor
        self._path = path
        self._max_bytes = max_bytes
        self._opened = os.fstat(descriptor)
        self._total = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        remaining = self._max_bytes - self._total
        chunk = os.read(self._descriptor, min(len(buffer), remaining + 1))
        if len(chunk) > remaining:
            raise DataError(f"csv source exceeds max_bytes={self._max_bytes}: {self._path}")
        buffer[: len(chunk)] = chunk
        self._total += len(chunk)
        return len(chunk)

    def validate_stable(self) -> None:
        final = os.fstat(self._descriptor)
        if (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns) != (
            self._opened.st_dev,
            self._opened.st_ino,
            self._opened.st_size,
            self._opened.st_mtime_ns,
        ):
            raise DataError(f"csv source changed while reading: {self._path}")

    def close(self) -> None:
        if not self.closed:
            os.close(self._descriptor)
        super().close()


@dataclass(frozen=True, slots=True, kw_only=True)
class _CsvFile:
    key: str
    label: str
    path: Path
    headers: list[str]
    delimiter: str
    max_bytes: int
    max_rows: int

    @property
    def identity_header(self) -> str | None:
        """The header acting as the identity, or ``None`` when synthesized."""
        return next((header for header in self.headers if header.lower() == "id"), None)


class CsvSource:
    provider = "csv"
    binding_kind = "files"

    async def discover_objects(self, credentials: Credentials) -> list[SourceObject]:
        parsed = self._metadata(credentials)
        return [
            SourceObject(
                key=parsed.key,
                label=parsed.label,
                canonical_type=parsed.key,
                default_selected=True,
            )
        ]

    async def inventory(
        self,
        credentials: Credentials,
        *,
        object_types: list[str] | None = None,
    ) -> Inventory:
        parsed = self._metadata(credentials)
        synthesize = parsed.identity_header is None
        column_types: dict[str, set[str]] = {}
        record_count = 0
        for row in self._iter_rows(parsed):
            record_count += 1
            _observe_column_types(column_types, row)

        warnings: list[str] = []
        fields: list[FieldSchema] = []
        if synthesize:
            fields.append(
                FieldSchema(
                    key=_ROW_FIELD, label="Row", data_type="number", required=True, unique=True
                )
            )
            warnings.append(
                "no 'id' column; the 1-based row index is exposed as field "
                f"{_ROW_FIELD!r} and synthesized as the identity"
            )
        for header in parsed.headers:
            if synthesize and header == _ROW_FIELD:
                warnings.append(
                    f"column {_ROW_FIELD!r} collides with the synthesized identity field; ignored"
                )
                continue
            observed = column_types.get(header, set())
            if len(observed) > 1:
                warnings.append(
                    f"column {header!r} has mixed types ({', '.join(sorted(observed))})"
                )
            fields.append(FieldSchema(key=header, label=header, data_type=_pick_type(observed)))

        identity = parsed.identity_header
        return Inventory(
            provider=self.provider,
            connection_id=credentials.connection_id,
            objects=[
                ObjectSchema(
                    key=parsed.key,
                    label=parsed.label,
                    canonical_type=parsed.key,
                    record_count=record_count,
                    fields=fields,
                    identity_fields=[identity if identity is not None else _ROW_FIELD],
                )
            ],
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
        parsed = self._metadata(credentials)
        if object_type != parsed.key:
            raise DataError(f"csv source has no object type {object_type!r}")
        synthesize = parsed.identity_header is None
        start = int(cursor) if cursor else 0
        page_size = max(1, limit)
        page: list[dict[str, str]] = []
        for index, row in enumerate(self._iter_rows(parsed)):
            if index < start:
                continue
            if len(page) >= page_size + 1:
                break
            page.append(row)
        has_more = len(page) > page_size
        page = page[:page_size]
        next_start = start + len(page)
        records: list[dict[str, Any]] = []
        for offset, row in enumerate(page):
            full: dict[str, Any] = dict(row)
            if synthesize:
                full[_ROW_FIELD] = start + offset + 1
            records.append({key: full.get(key) for key in field_keys})
        return RecordPage(
            object_key=object_type,
            records=records,
            next_cursor=str(next_start) if has_more else None,
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
        parsed = self._metadata(credentials)
        if object_type != parsed.key:
            raise DataError(f"csv source has no object type {object_type!r}")
        return sum(1 for _ in self._iter_rows(parsed))

    # -- internals ----------------------------------------------------------

    def _metadata(self, credentials: Credentials) -> _CsvFile:
        path = self._path(credentials)
        max_bytes = _positive_limit(credentials, "max_bytes", _DEFAULT_MAX_BYTES)
        max_rows = _positive_limit(credentials, "max_rows", _DEFAULT_MAX_ROWS)
        try:
            with _open_regular_text(path, max_bytes=max_bytes) as stream:
                sample = stream.read(_SNIFF_SAMPLE_CHARS)
            delimiter = _delimiter(path, sample)
            with _open_regular_text(path, max_bytes=max_bytes) as stream:
                rows = csv.reader(stream, delimiter=delimiter)
                header_row = next((row for row in rows if row), None)
        except UnicodeDecodeError as error:
            raise DataError(f"csv source file is not UTF-8 text: {path}") from error
        if header_row is None:
            raise ConfigurationError(f"csv source file has no header row: {path}")
        headers = [header.strip() for header in header_row]
        if any(not header for header in headers):
            raise ConfigurationError(f"csv source header row has empty column names: {path}")
        canonical = [header.casefold() for header in headers]
        if len(set(canonical)) != len(canonical):
            raise ConfigurationError(
                f"csv source header row has duplicate canonical column names: {path}"
            )
        if not any(header == "id" for header in canonical) and _ROW_FIELD in canonical:
            raise ConfigurationError(
                f"csv source column {_ROW_FIELD!r} collides with the synthesized identity: {path}"
            )
        return _CsvFile(
            key=_object_key(path.stem),
            label=path.stem,
            path=path,
            headers=headers,
            delimiter=delimiter,
            max_bytes=max_bytes,
            max_rows=max_rows,
        )

    def _iter_rows(self, parsed: _CsvFile) -> Iterator[dict[str, str]]:
        try:
            with _open_regular_text(parsed.path, max_bytes=parsed.max_bytes) as stream:
                rows = csv.reader(stream, delimiter=parsed.delimiter)
                header_row = next((row for row in rows if row), None)
                headers = [] if header_row is None else [header.strip() for header in header_row]
                if headers != parsed.headers:
                    raise DataError(f"csv source header changed while reading: {parsed.path}")
                count = 0
                for row in rows:
                    if not row:
                        continue
                    count += 1
                    if count > parsed.max_rows:
                        raise DataError(
                            f"csv source exceeds max_rows={parsed.max_rows}: {parsed.path}"
                        )
                    yield dict(zip(parsed.headers, row, strict=False))
        except UnicodeDecodeError as error:
            raise DataError(f"csv source file is not UTF-8 text: {parsed.path}") from error

    def _path(self, credentials: Credentials) -> Path:
        raw = credentials.settings.get("connection") or credentials.settings.get("path")
        if not raw:
            raise ConfigurationError("csv source needs a file path (connection)")
        path = Path(str(raw)).expanduser()
        return path


def _object_key(stem: str) -> str:
    normalized = _KEY_SANITIZER.sub("_", stem.strip().lower()).strip("_")
    if not normalized:
        raise ConfigurationError(f"cannot derive an object key from file name {stem!r}")
    return normalized


def _delimiter(path: Path, sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=_SNIFF_DELIMITERS).delimiter
    except csv.Error:
        return "\t" if path.suffix.lower() == ".tsv" else ","


def _observe_column_types(types: dict[str, set[str]], row: dict[str, str]) -> None:
    for header, cell in row.items():
        stripped = cell.strip()
        if stripped:
            types.setdefault(header, set()).add(_cell_type(stripped))


def _positive_limit(credentials: Credentials, key: str, default: int) -> int:
    raw = credentials.settings.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"csv source {key} must be a positive integer") from error
    if value <= 0:
        raise ConfigurationError(f"csv source {key} must be a positive integer")
    return value


@contextmanager
def _open_regular_text(path: Path, *, max_bytes: int) -> Iterator[TextIO]:
    absolute = path.absolute()
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fds: list[int] = []
    descriptor: int | None = None
    raw: _BoundedDescriptorReader | None = None
    stream: io.TextIOWrapper | None = None
    try:
        parent_fd = os.open(absolute.anchor, directory_flags)
        directory_fds.append(parent_fd)
        for component in absolute.parent.parts[1:]:
            parent_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            directory_fds.append(parent_fd)
        descriptor = os.open(absolute.name, file_flags, dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise DataError(f"csv source path is not a regular file: {path}")
        if opened.st_size > max_bytes:
            raise DataError(f"csv source exceeds max_bytes={max_bytes}: {path}")
        raw = _BoundedDescriptorReader(descriptor, path=path, max_bytes=max_bytes)
        descriptor = None
        stream = io.TextIOWrapper(io.BufferedReader(raw), encoding="utf-8-sig", newline="")
        yield stream
        raw.validate_stable()
    except FileNotFoundError as error:
        raise ConfigurationError(f"csv source file not found: {path}") from error
    except OSError as error:
        if any(component.is_symlink() for component in (absolute, *absolute.parents)):
            raise DataError(f"csv source path contains a symbolic link: {path}") from error
        raise DataError(f"csv source path cannot be opened safely: {path}") from error
    finally:
        if stream is not None:
            stream.close()
        elif raw is not None:
            raw.close()
        if descriptor is not None:
            os.close(descriptor)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


def _cell_type(text: str) -> str:
    if text.lower() in ("true", "false"):
        return "boolean"
    try:
        float(text)
    except ValueError:
        return "string"
    return "number"


def _pick_type(types: set[str]) -> str:
    return next(iter(types)) if len(types) == 1 else "string"


def _reject_filter(source_filter: SourceFilter | None) -> None:
    if source_filter is not None:
        raise UnsupportedFeatureError(
            "csv source filters are not supported",
            remediation="remove the source filter or use a connector that supports it",
        )


CONNECTOR = ConnectorRegistration(name="csv", source=CsvSource())

# SPDX-License-Identifier: Apache-2.0
"""Markdown directory source connector.

Each ``*.md`` file becomes one record: ``path`` (relative, the identity),
``slug``, ``content`` (the body), plus every frontmatter key. Files are
scanned in sorted order so pagination cursors are deterministic.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

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

_SECURE_OPEN_AVAILABLE = hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd

_RESERVED_FIELDS = ("path", "slug", "content")


@dataclass(frozen=True, slots=True)
class _Document:
    path: str
    slug: str
    content: str
    frontmatter: dict[str, Any]
    frontmatter_error: bool


class MarkdownSource:
    provider = "markdown"
    binding_kind = "files"

    async def discover_objects(self, credentials: Credentials) -> list[SourceObject]:
        self._root(credentials)
        return [
            SourceObject(
                key="documents",
                label="Documents",
                canonical_type="documents",
                default_selected=True,
            )
        ]

    async def inventory(
        self,
        credentials: Credentials,
        *,
        object_types: list[str] | None = None,
    ) -> Inventory:
        documents = self._scan(self._root(credentials))
        field_types: dict[str, set[str]] = {}
        for document in documents:
            for key, value in document.frontmatter.items():
                field_types.setdefault(key, set()).add(_type_name(value))

        warnings: list[str] = []
        parse_errors = sum(1 for d in documents if d.frontmatter_error)
        if parse_errors:
            warnings.append(
                f"{parse_errors} file(s) have unparseable frontmatter; treated as body-only"
            )
        fields = [
            FieldSchema(key="path", label="Path", data_type="string", required=True, unique=True),
            FieldSchema(key="slug", label="Slug", data_type="string"),
            FieldSchema(key="content", label="Content", data_type="text"),
        ]
        for key in sorted(field_types):
            if key in _RESERVED_FIELDS:
                warnings.append(
                    f"frontmatter field {key!r} collides with a reserved field; ignored"
                )
                continue
            types = field_types[key]
            if len(types) > 1:
                warnings.append(
                    f"frontmatter field {key!r} has mixed types ({', '.join(sorted(types))})"
                )
            fields.append(FieldSchema(key=key, label=key, data_type=_pick_type(types)))

        return Inventory(
            provider=self.provider,
            connection_id=credentials.connection_id,
            objects=[
                ObjectSchema(
                    key="documents",
                    label="Documents",
                    canonical_type="documents",
                    record_count=len(documents),
                    fields=fields,
                    identity_fields=["path"],
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
        if object_type != "documents":
            raise DataError(f"markdown source has no object type {object_type!r}")
        documents = self._scan(self._root(credentials))
        start = int(cursor) if cursor else 0
        page = documents[start : start + max(1, limit)]
        next_start = start + len(page)
        records = []
        for document in page:
            full: dict[str, Any] = {
                "path": document.path,
                "slug": document.slug,
                "content": document.content,
                **{k: v for k, v in document.frontmatter.items() if k not in _RESERVED_FIELDS},
            }
            records.append({key: full.get(key) for key in field_keys})
        return RecordPage(
            object_key=object_type,
            records=records,
            next_cursor=str(next_start) if next_start < len(documents) else None,
            has_more=next_start < len(documents),
        )

    async def count_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
    ) -> int:
        _reject_filter(source_filter)
        if object_type != "documents":
            raise DataError(f"markdown source has no object type {object_type!r}")
        return len(self._scan(self._root(credentials)))

    def _root(self, credentials: Credentials) -> Path:
        raw = credentials.settings.get("connection") or credentials.settings.get("path")
        if not raw:
            raise ConfigurationError("markdown source needs a directory path (connection)")
        root = Path(str(raw)).expanduser()
        if not root.is_dir():
            raise ConfigurationError(f"markdown source directory not found: {root}")
        return root

    def _scan(self, root: Path) -> list[_Document]:
        resolved_root = root.resolve(strict=True)
        documents: list[_Document] = []
        for file_path in sorted(resolved_root.rglob("*.md")):
            relative_path = file_path.relative_to(resolved_root)
            if file_path.is_symlink():
                raise DataError(
                    "markdown source contains a symbolic-link file outside its trusted "
                    f"file boundary: {relative_path}"
                )
            text = _read_confined_text(resolved_root, relative_path)
            frontmatter, content, failed = _split_frontmatter(text)
            documents.append(
                _Document(
                    path=relative_path.as_posix(),
                    slug=file_path.stem,
                    content=content,
                    frontmatter=frontmatter,
                    frontmatter_error=failed,
                )
            )
        return documents


def _read_confined_text(root: Path, relative_path: Path) -> str:
    """Read one regular file without following a replaced path component."""

    if (
        not relative_path.parts
        or relative_path.is_absolute()
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        raise DataError(f"markdown source path cannot be opened safely: {relative_path}")
    if not _SECURE_OPEN_AVAILABLE:
        if os.name == "nt":
            return _read_confined_text_windows(root, relative_path)
        raise DataError(f"markdown source path cannot be opened safely: {relative_path}")

    nofollow = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    directory_flags = nofollow | getattr(os, "O_DIRECTORY", 0)
    directory_fds: list[int] = []
    file_fd: int | None = None
    try:
        root_fd = os.open(root, directory_flags)
        directory_fds.append(root_fd)
        parent_fd = root_fd
        for component in relative_path.parts[:-1]:
            parent_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            directory_fds.append(parent_fd)
        file_fd = os.open(relative_path.parts[-1], nofollow, dir_fd=parent_fd)
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise DataError(f"markdown source path is not a regular file: {relative_path}")
        with os.fdopen(file_fd, encoding="utf-8") as stream:
            file_fd = None
            return stream.read()
    except OSError as error:
        raise DataError(f"markdown source path cannot be opened safely: {relative_path}") from error
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for descriptor in reversed(directory_fds):
            os.close(descriptor)


def _read_confined_text_windows(root: Path, relative_path: Path) -> str:
    """Validate the final Windows handle before reading through it."""

    import ctypes
    import importlib

    windows_api: Any = ctypes
    msvcrt_api: Any = importlib.import_module("msvcrt")

    file_fd: int | None = None
    try:
        file_fd = os.open(
            root / relative_path,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0),
        )
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise DataError(f"markdown source path is not a regular file: {relative_path}")

        buffer = ctypes.create_unicode_buffer(32768)
        kernel32 = windows_api.WinDLL("kernel32", use_last_error=True)
        written = kernel32.GetFinalPathNameByHandleW(
            msvcrt_api.get_osfhandle(file_fd), buffer, len(buffer), 0
        )
        if written == 0 or written >= len(buffer):
            raise OSError(
                windows_api.get_last_error(),
                "could not resolve the opened file handle",
            )
        opened_path = buffer.value
        if opened_path.startswith("\\\\?\\UNC\\"):
            opened_path = "\\\\" + opened_path[8:]
        elif opened_path.startswith("\\\\?\\"):
            opened_path = opened_path[4:]
        expected_root = os.path.normcase(os.path.abspath(root))
        opened_path = os.path.normcase(os.path.abspath(opened_path))
        if os.path.commonpath([expected_root, opened_path]) != expected_root:
            raise DataError(
                f"markdown source file resolves outside the configured directory: {relative_path}"
            )

        with os.fdopen(file_fd, encoding="utf-8") as stream:
            file_fd = None
            return stream.read()
    except OSError as error:
        raise DataError(f"markdown source path cannot be opened safely: {relative_path}") from error
    finally:
        if file_fd is not None:
            os.close(file_fd)


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str, bool]:
    if not text.startswith("---\n"):
        return {}, text, False
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text, True
    raw_frontmatter = text[4:end]
    body = text[end + 4 :].lstrip("\n")
    try:
        loaded = yaml.safe_load(raw_frontmatter)
    except yaml.YAMLError:
        return {}, body, True
    if loaded is None:
        return {}, body, False
    if not isinstance(loaded, dict):
        return {}, body, True
    return {str(k): v for k, v in loaded.items()}, body, False


def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "string"


def _pick_type(types: set[str]) -> str:
    return next(iter(types)) if len(types) == 1 else "string"


def _reject_filter(source_filter: SourceFilter | None) -> None:
    if source_filter is not None:
        raise UnsupportedFeatureError(
            "markdown source filters are not supported",
            remediation="remove the source filter or use a connector that supports it",
        )


CONNECTOR = ConnectorRegistration(name="markdown", source=MarkdownSource())

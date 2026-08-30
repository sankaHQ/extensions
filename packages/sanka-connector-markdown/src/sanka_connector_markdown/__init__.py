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
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

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
_DEFAULT_MAX_FILES = 100_000
_DEFAULT_MAX_ENTRIES = 200_000
_DEFAULT_MAX_DEPTH = 64
_DEFAULT_MAX_FILE_BYTES = 8 * 1024 * 1024


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from error
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


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
        root = self._root(credentials)
        paths = self._paths(root, credentials)
        field_types: dict[str, set[str]] = {}
        parse_errors = 0
        for path in paths:
            document = self._read_document(
                root,
                path,
                max_file_bytes=_positive_limit(
                    credentials, "max_file_bytes", _DEFAULT_MAX_FILE_BYTES
                ),
            )
            parse_errors += int(document.frontmatter_error)
            for key, value in document.frontmatter.items():
                field_types.setdefault(key, set()).add(_type_name(value))

        warnings: list[str] = []
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
                    record_count=len(paths),
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
        root = self._root(credentials)
        paths = self._paths(root, credentials)
        start = int(cursor) if cursor else 0
        page_paths = paths[start : start + max(1, limit)]
        next_start = start + len(page_paths)
        max_file_bytes = _positive_limit(credentials, "max_file_bytes", _DEFAULT_MAX_FILE_BYTES)
        records = []
        for path in page_paths:
            document = self._read_document(root, path, max_file_bytes=max_file_bytes)
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
            next_cursor=str(next_start) if next_start < len(paths) else None,
            has_more=next_start < len(paths),
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
        return len(self._paths(self._root(credentials), credentials))

    def _root(self, credentials: Credentials) -> Path:
        raw = credentials.settings.get("connection") or credentials.settings.get("path")
        if not raw:
            raise ConfigurationError("markdown source needs a directory path (connection)")
        root = Path(str(raw)).expanduser()
        if not root.is_dir():
            raise ConfigurationError(f"markdown source directory not found: {root}")
        return root

    def _paths(self, root: Path, credentials: Credentials) -> list[Path]:
        resolved_root = root.resolve(strict=True)
        max_files = _positive_limit(credentials, "max_files", _DEFAULT_MAX_FILES)
        max_entries = _positive_limit(credentials, "max_entries", _DEFAULT_MAX_ENTRIES)
        max_depth = _positive_limit(credentials, "max_depth", _DEFAULT_MAX_DEPTH)
        paths: list[Path] = []
        directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        entries = 0

        def walk(directory_fd: int, relative_directory: Path, depth: int) -> None:
            nonlocal entries
            if depth > max_depth:
                raise DataError(f"markdown source exceeds max_depth={max_depth}: {resolved_root}")
            with os.scandir(directory_fd) as iterator:
                for entry in iterator:
                    entries += 1
                    if entries > max_entries:
                        raise DataError(
                            f"markdown source exceeds max_entries={max_entries}: {resolved_root}"
                        )
                    relative_path = relative_directory / entry.name
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        if entry.name.lower().endswith(".md"):
                            raise DataError(
                                "markdown source contains a symbolic-link file outside "
                                f"its trusted file boundary: {relative_path}"
                            )
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        child_fd = os.open(entry.name, directory_flags, dir_fd=directory_fd)
                        try:
                            walk(child_fd, relative_path, depth + 1)
                        finally:
                            os.close(child_fd)
                    elif stat.S_ISREG(info.st_mode) and entry.name.lower().endswith(".md"):
                        paths.append(relative_path)
                        if len(paths) > max_files:
                            raise DataError(
                                f"markdown source exceeds max_files={max_files}: {resolved_root}"
                            )

        root_fd = os.open(resolved_root, directory_flags)
        try:
            walk(root_fd, Path(), 0)
        except OSError as error:
            raise DataError(
                f"markdown source tree cannot be enumerated safely: {resolved_root}"
            ) from error
        finally:
            os.close(root_fd)
        return sorted(paths)

    def _read_document(self, root: Path, relative_path: Path, *, max_file_bytes: int) -> _Document:
        resolved_root = root.resolve(strict=True)
        text = _read_confined_text(resolved_root, relative_path, max_file_bytes=max_file_bytes)
        frontmatter, content, failed = _split_frontmatter(text)
        return _Document(
            path=relative_path.as_posix(),
            slug=relative_path.stem,
            content=content,
            frontmatter=frontmatter,
            frontmatter_error=failed,
        )


def _read_confined_text(root: Path, relative_path: Path, *, max_file_bytes: int) -> str:
    """Read one regular file without following a replaced path component."""

    if (
        not relative_path.parts
        or relative_path.is_absolute()
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        raise DataError(f"markdown source path cannot be opened safely: {relative_path}")
    if not _SECURE_OPEN_AVAILABLE:
        if os.name == "nt":
            return _read_confined_text_windows(root, relative_path, max_file_bytes=max_file_bytes)
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
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise DataError(f"markdown source path is not a regular file: {relative_path}")
        if opened.st_size > max_file_bytes:
            raise DataError(
                f"markdown source file exceeds max_file_bytes={max_file_bytes}: {relative_path}"
            )
        return _read_bounded_descriptor(
            file_fd,
            relative_path=relative_path,
            max_file_bytes=max_file_bytes,
        )
    except OSError as error:
        raise DataError(f"markdown source path cannot be opened safely: {relative_path}") from error
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for descriptor in reversed(directory_fds):
            os.close(descriptor)


def _read_confined_text_windows(root: Path, relative_path: Path, *, max_file_bytes: int) -> str:
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
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise DataError(f"markdown source path is not a regular file: {relative_path}")
        if opened.st_size > max_file_bytes:
            raise DataError(
                f"markdown source file exceeds max_file_bytes={max_file_bytes}: {relative_path}"
            )

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

        return _read_bounded_descriptor(
            file_fd,
            relative_path=relative_path,
            max_file_bytes=max_file_bytes,
        )
    except OSError as error:
        raise DataError(f"markdown source path cannot be opened safely: {relative_path}") from error
    finally:
        if file_fd is not None:
            os.close(file_fd)


def _read_bounded_descriptor(
    descriptor: int,
    *,
    relative_path: Path,
    max_file_bytes: int,
) -> str:
    opened = os.fstat(descriptor)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(1024 * 1024, max_file_bytes - total + 1))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_file_bytes:
            raise DataError(
                f"markdown source file exceeds max_file_bytes={max_file_bytes}: {relative_path}"
            )
    final = os.fstat(descriptor)
    if (final.st_dev, final.st_ino, final.st_size) != (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
    ):
        raise DataError(f"markdown source file changed while reading: {relative_path}")
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as error:
        raise DataError(f"markdown source file is not UTF-8: {relative_path}") from error


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str, bool]:
    if not text.startswith("---\n"):
        return {}, text, False
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text, True
    raw_frontmatter = text[4:end]
    body = text[end + 4 :].lstrip("\n")
    try:
        loaded = yaml.load(raw_frontmatter, Loader=_UniqueKeyLoader)
    except ConstructorError as error:
        raise DataError(
            "markdown frontmatter contains duplicate or invalid mapping keys"
        ) from error
    except yaml.YAMLError:
        return {}, body, True
    if loaded is None:
        return {}, body, False
    if not isinstance(loaded, dict):
        return {}, body, True
    canonical: dict[str, Any] = {}
    for raw_key, value in loaded.items():
        key = str(raw_key)
        if key in canonical:
            raise DataError(f"markdown frontmatter keys collide after string conversion: {key!r}")
        if key in _RESERVED_FIELDS:
            raise DataError(f"markdown frontmatter key collides with reserved field: {key!r}")
        canonical[key] = value
    return canonical, body, False


def _positive_limit(credentials: Credentials, key: str, default: int) -> int:
    raw = credentials.settings.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"markdown source {key} must be a positive integer") from error
    if value <= 0:
        raise ConfigurationError(f"markdown source {key} must be a positive integer")
    return value


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

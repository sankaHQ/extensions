# SPDX-License-Identifier: Apache-2.0
"""Canonical hashes and contained, symlink-free project reads."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

IGNORED = {".git", ".sanka", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def object_digest(value: Any) -> str:
    return digest(canonical(value))


def safe_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(part in {"..", "."} for part in path.parts):
        raise ValueError("path must be contained and relative")
    if root.is_symlink() or root.resolve() != root.absolute():
        raise ValueError("project root must not contain symlinks")
    result = root
    for part in path.parts:
        result /= part
        if result.is_symlink():
            raise ValueError(f"symlink is unsupported: {relative}")
    if not result.resolve().is_relative_to(root.resolve()):
        raise ValueError("path escapes root")
    return result


def source_files(root: Path) -> dict[str, str]:
    if not root.is_dir() or root.is_symlink() or root.resolve() != root.absolute():
        raise ValueError("source must be a real directory without symlinks")
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in IGNORED for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"source contains symlink: {relative.as_posix()}")
        if path.is_file():
            result[relative.as_posix()] = digest(path.read_bytes())
    return result

# SPDX-License-Identifier: Apache-2.0
"""Run the vendored TypeScript compiler as a bounded, network-free syntax driver.

The driver parses or transpiles exactly the texts it is given. It never resolves
imports, reads other files or executes project code. Node.js is required; the
compiler bundle is vendored and digest-checked on every invocation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TYPESCRIPT_VERSION = "5.9.3"
TYPESCRIPT_SHA256 = "3ae902c92cc44dace175c0e69e13a4b0899f6983c6121d76b9ab8dd5795e7675"
MIN_NODE_MAJOR = 20
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
TIMEOUT_SECONDS = 120
NODE_ROOT = Path(__file__).resolve().parent / "node"
_VERSION = re.compile(r"\Av(\d+)\.(\d+)\.(\d+)")


class TypeScriptDriverError(ValueError):
    """The syntax driver could not run or returned an invalid result."""


@dataclass(frozen=True)
class Diagnostic:
    code: int
    message: str
    start: int | None
    length: int | None


@dataclass(frozen=True)
class ParsedFile:
    path: str
    script_kind: str
    diagnostics: tuple[Diagnostic, ...]
    tree: dict[str, Any]


def node_executable() -> str:
    """Return the Node.js executable: ``SANKA_NODE`` when set, otherwise ``node`` on PATH."""
    explicit = os.environ.get("SANKA_NODE")
    if explicit:
        path = Path(explicit)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise TypeScriptDriverError("SANKA_NODE must point to an executable Node.js binary")
        return str(path)
    found = shutil.which("node")
    if found is None:
        raise TypeScriptDriverError(
            "Node.js is required to parse TypeScript; install Node.js 22 or set SANKA_NODE"
        )
    return found


def node_version(executable: str) -> tuple[int, int, int]:
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TypeScriptDriverError(f"Node.js version check failed: {error}") from error
    match = _VERSION.match(result.stdout.strip())
    if result.returncode or match is None:
        raise TypeScriptDriverError("Node.js version check failed")
    major, minor, patch = (int(item) for item in match.groups())
    return major, minor, patch


def bundle_path() -> Path:
    """Return the vendored compiler bundle after checking its pinned digest."""
    path = NODE_ROOT / "typescript.js"
    if not path.is_file():
        raise TypeScriptDriverError(
            "vendored TypeScript bundle is missing; in a source checkout run "
            "scripts/fetch_typescript_bundle.py (wheels include it)"
        )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != TYPESCRIPT_SHA256:
        raise TypeScriptDriverError(
            "vendored TypeScript bundle digest does not match the pinned release"
        )
    return path


def parse_sources(files: Mapping[str, str], *, node: str | None = None) -> dict[str, ParsedFile]:
    """Parse each text and return its syntax tree and diagnostics keyed by path."""
    payload = _run("parse", files, node)
    parsed: dict[str, ParsedFile] = {}
    for item in _items(payload):
        diagnostics = tuple(
            Diagnostic(
                int(entry["code"]),
                str(entry["message"]),
                _optional_int(entry.get("start")),
                _optional_int(entry.get("length")),
            )
            for entry in _diagnostics(item)
        )
        tree = item.get("tree")
        if not isinstance(tree, dict):
            raise TypeScriptDriverError("TypeScript driver returned an invalid syntax tree")
        path = str(item["path"])
        parsed[path] = ParsedFile(path, str(item.get("kind", "")), diagnostics, tree)
    if set(parsed) != set(files):
        raise TypeScriptDriverError("TypeScript driver did not return every file")
    return parsed


def transpile_sources(files: Mapping[str, str], *, node: str | None = None) -> dict[str, str]:
    """Transpile each text to CommonJS JavaScript without type checking."""
    payload = _run("transpile", files, node)
    outputs: dict[str, str] = {}
    for item in _items(payload):
        diagnostics = _diagnostics(item)
        if diagnostics:
            first = diagnostics[0]
            raise TypeScriptDriverError(
                f"{item['path']}: syntax error {first['code']}: {first['message']}"
            )
        output = item.get("output")
        if not isinstance(output, str):
            raise TypeScriptDriverError("TypeScript driver returned invalid transpiled output")
        outputs[str(item["path"])] = output
    if set(outputs) != set(files):
        raise TypeScriptDriverError("TypeScript driver did not return every file")
    return outputs


def _environment() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", ""), "NODE_OPTIONS": ""}


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _validate(files: Mapping[str, str]) -> list[dict[str, str]]:
    if not files:
        raise TypeScriptDriverError("at least one file is required")
    total = 0
    items: list[dict[str, str]] = []
    for path in sorted(files):
        text = files[path]
        if type(path) is not str or not path or type(text) is not str:
            raise TypeScriptDriverError("files must map non-empty paths to text")
        size = len(text.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise TypeScriptDriverError(f"{path} exceeds the {MAX_FILE_BYTES}-byte capture limit")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise TypeScriptDriverError("files exceed the total capture limit")
        items.append({"path": path, "text": text})
    return items


def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("files")
    if not isinstance(items, list) or not all(
        isinstance(item, dict) and isinstance(item.get("path"), str) for item in items
    ):
        raise TypeScriptDriverError("TypeScript driver returned an invalid file list")
    return items


def _diagnostics(item: dict[str, Any]) -> list[dict[str, Any]]:
    diagnostics = item.get("diagnostics", [])
    if not isinstance(diagnostics, list) or not all(
        isinstance(entry, dict) and isinstance(entry.get("code"), int) for entry in diagnostics
    ):
        raise TypeScriptDriverError("TypeScript driver returned invalid diagnostics")
    return diagnostics


def _run(command: str, files: Mapping[str, str], node: str | None) -> dict[str, Any]:
    items = _validate(files)
    executable = node or node_executable()
    major = node_version(executable)[0]
    if major < MIN_NODE_MAJOR:
        raise TypeScriptDriverError(
            f"Node.js {MIN_NODE_MAJOR} or later is required to parse TypeScript (found {major})"
        )
    bundle_path()
    request = json.dumps({"command": command, "files": items}, ensure_ascii=False, allow_nan=False)
    try:
        result = subprocess.run(
            [executable, str(NODE_ROOT / "parse.js")],
            input=request.encode("utf-8"),
            capture_output=True,
            cwd=NODE_ROOT,
            env=_environment(),
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TypeScriptDriverError(f"TypeScript driver failed: {error}") from error
    if len(result.stdout) > MAX_OUTPUT_BYTES:
        raise TypeScriptDriverError("TypeScript driver output exceeds its limit")
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except ValueError as error:
        detail = result.stderr.decode("utf-8", errors="replace")[-2000:]
        raise TypeScriptDriverError(
            f"TypeScript driver returned invalid output: {detail}"
        ) from error
    if not isinstance(payload, dict):
        raise TypeScriptDriverError("TypeScript driver returned an invalid document")
    if result.returncode or "error" in payload:
        raise TypeScriptDriverError(
            f"TypeScript driver failed: {payload.get('error', 'unknown error')}"
        )
    if payload.get("typescript") != TYPESCRIPT_VERSION:
        raise TypeScriptDriverError("TypeScript driver version does not match the pinned release")
    return payload

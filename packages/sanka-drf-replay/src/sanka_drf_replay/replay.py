# SPDX-License-Identifier: Apache-2.0
"""Differential scenario replay: the source Django application against a candidate.

Python candidates expose ``app``; Go candidates expose an HTTP server using PORT.
Every scenario starts from an identical freshly migrated (and optionally seeded)
SQLite database, is sent to both applications, and the responses and the resulting
database state are compared. Nothing here depends on a Sanka plan or a generated
manifest, so the replay works at any readiness, including zero.

Request and response semantics deliberately match the Sanka Migration Bench
evaluator: the source side uses Django's test client with CSRF enforcement and a
JSON content type, the candidate uses its framework test client without
following redirects, declared headers are compared lower-cased, bodies compare as
JSON when both parse and as bytes otherwise, and multipart bodies are encoded with
the bench's fixed boundary.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from itertools import zip_longest
from pathlib import Path
from typing import Any

from ._scripts import _CANDIDATE_SCRIPT, _PREPARE_SCRIPT, _SOURCE_SCRIPT
from ._scripts import _REQUEST_SCRIPT as _REQUEST_SCRIPT

REPLAY_SCHEMA = "sanka-verify-replay/v1"
DEFAULT_DB_ENV = "SANKA_TEST_DB"
DEFAULT_ENTRYPOINT = "target_app.py"
DEFAULT_IGNORED_TABLES: tuple[str, ...] = (
    "django_admin_log",
    "django_migrations",
    "django_session",
    "sqlite_sequence",
)
EDGE_HEADERS: tuple[str, ...] = ("allow", "location", "www-authenticate")
VOLATILE_HEADERS: frozenset[str] = frozenset({"date", "server"})
HTTP_METHODS: frozenset[str] = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"}
)
_MULTIPART_BOUNDARY = "SankaBenchBoundary"
_SIDE_TIMEOUT_SECONDS = 300


class ReplayError(Exception):
    """A scenario file, environment, or side process problem that is not a mismatch."""

    def __init__(self, message: str, *, category: str = "infrastructure_failure"):
        super().__init__(message)
        self.category = category


# ---------------------------------------------------------------------------
# Scenario loading


def load_scenarios(path: Path) -> list[dict[str, Any]]:
    """Load and validate a scenario file (a list, or an object with ``scenarios``)."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReplayError(f"could not read scenarios from {path}: {error}") from error
    items = payload.get("scenarios") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ReplayError("scenarios must be a JSON array or an object with a `scenarios` array")
    if not items:
        raise ReplayError("scenarios must not be empty")
    scenarios: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        scenario = _validated_request(item, f"scenarios[{index}]", require_id=True)
        identifier = str(scenario["id"])
        if identifier in seen:
            raise ReplayError(f"duplicate scenario id: {identifier}")
        seen.add(identifier)
        scenarios.append(scenario)
    return scenarios


def _validated_request(item: object, label: str, *, require_id: bool) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ReplayError(f"{label} must be an object")
    method = str(item.get("method", "GET")).upper()
    if method not in HTTP_METHODS:
        raise ReplayError(f"{label}.method is not an HTTP method: {method}")
    path = item.get("path")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ReplayError(f"{label}.path must start with '/'")
    headers = item.get("headers") or {}
    if not isinstance(headers, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
    ):
        raise ReplayError(f"{label}.headers must map strings to strings")
    if sum(name in item for name in ("body", "body_base64", "multipart")) > 1:
        raise ReplayError(f"{label} must use only one body encoding")
    if "body_base64" in item:
        try:
            base64.b64decode(item["body_base64"], validate=True)
        except (ValueError, TypeError) as error:
            raise ReplayError(f"{label}.body_base64 must be valid base64") from error
    multipart = item.get("multipart")
    if multipart is not None and not isinstance(multipart, dict):
        raise ReplayError(f"{label}.multipart must be an object")
    capture = item.get("capture_headers") or []
    if not isinstance(capture, list) or not all(isinstance(name, str) for name in capture):
        raise ReplayError(f"{label}.capture_headers must be an array of header names")
    response_body = item.get("response_body")
    if response_body not in (None, "base64"):
        raise ReplayError(f"{label}.response_body must be omitted or 'base64'")
    if "expected_source_status" in item:
        if not require_id:
            raise ReplayError(
                f"{label}.expected_source_status is supported only on top-level scenarios"
            )
        expected = item["expected_source_status"]
        if type(expected) is not int or not 100 <= expected <= 599:
            raise ReplayError(f"{label}.expected_source_status must be an integer HTTP status")
    setup_items = item.get("setup") or []
    if not isinstance(setup_items, list):
        raise ReplayError(f"{label}.setup must be an array of requests")
    setup = [
        _validated_request(step, f"{label}.setup[{position}]", require_id=False)
        for position, step in enumerate(setup_items)
    ]
    validated: dict[str, Any] = {
        "method": method,
        "path": path,
        "headers": {key.lower(): value for key, value in headers.items()},
        **({"body": item["body"]} if "body" in item else {}),
        **({"body_base64": item["body_base64"]} if "body_base64" in item else {}),
        "multipart": multipart,
        "capture_headers": [str(name).lower() for name in capture],
        "response_body": response_body,
        "setup": setup,
        **(
            {"expected_source_status": item["expected_source_status"]}
            if "expected_source_status" in item
            else {}
        ),
    }
    if require_id:
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise ReplayError(f"{label}.id must be a non-empty string")
        validated["id"] = identifier
    elif "id" in item:
        validated["id"] = str(item["id"])
    return validated


# ---------------------------------------------------------------------------
# Scan-derived edge probes


def edge_probes_from_scan(
    scan: Mapping[str, Any], scenarios: Sequence[Mapping[str, Any]] = ()
) -> list[dict[str, Any]]:
    """Derive edge requests, reusing a matching scenario's headers and fixture setup.

    Per concrete path: OPTIONS (Allow), one method the source does not declare
    (405 and Allow), the trailing-slash variant (redirect or 404 with Location),
    and, for single-parameter detail paths, a request for an object that does not
    exist (404 body). GET routes also receive a HEAD probe. Only provided setup
    requests may mutate fixture state. Context never crosses route boundaries.
    Supplied write bodies also support bounded mutations without captured routes;
    these requests do not establish static capture or generation support.
    """
    routes = scan.get("routes")
    if not isinstance(routes, list):
        routes = []
    methods_by_path: dict[str, set[str]] = {}
    for route in routes:
        if not isinstance(route, dict):
            continue
        method = route.get("method")
        path = route.get("path")
        if (not isinstance(method, str) or not isinstance(path, str)) and isinstance(
            route.get("key"), str
        ):
            method, _, path = str(route["key"]).partition(" ")
        if not isinstance(method, str) or not isinstance(path, str) or not path.startswith("/"):
            continue
        methods_by_path.setdefault(path, set()).add(method.upper())
    probes: list[dict[str, Any]] = []
    for path in sorted(methods_by_path):
        methods = methods_by_path[path]
        context = _probe_context(path, scenarios)
        concrete = str(context["path"]).split("?", 1)[0] if context else _concrete_path(path)
        if concrete is None:
            continue
        probes.append(_edge("options", "OPTIONS", concrete, path, context))
        if "GET" in methods:
            probes.append(_edge("head", "HEAD", concrete, path, context))
        unsupported = next(
            (
                candidate
                for candidate in ("TRACE", "PATCH", "PUT", "DELETE", "POST")
                if candidate not in methods
            ),
            None,
        )
        if unsupported is not None:
            probes.append(_edge("method-not-allowed", unsupported, concrete, path, context))
        variant = concrete[:-1] if concrete.endswith("/") and len(concrete) > 1 else concrete + "/"
        probes.append(_edge("slash-variant", "GET", variant, path, context))
        missing = _concrete_path(path)
        if "{" in path and missing is not None:
            probes.append(_edge("missing-object", "GET", missing, path, context))
    return probes + _contract_probes(scan, scenarios)


def _contract_probes(
    scan: Mapping[str, Any], scenarios: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Mutate supplied requests; the source, never an invented status, is the oracle."""
    serializers = {
        s["name"]: s
        for s in (scan.get("serializer_details") or [])
        if isinstance(s, dict) and isinstance(s.get("name"), str)
    }
    probes: list[dict[str, Any]] = []
    groups: list[list[dict[str, Any]]] = []
    for scenario in scenarios:
        method = str(scenario.get("method", "GET")).upper()
        route = next(
            (
                r
                for r in (scan.get("routes") or [])
                if isinstance(r, dict)
                and r.get("method") == method
                and isinstance(r.get("path"), str)
                and _probe_context(r["path"], [scenario])
            ),
            None,
        )
        request_only = route is None
        if route is None:
            # Supplied requests support replay even when static capture is unsupported.
            route = {"path": scenario["path"]}
        headers = {k.lower(): v for k, v in (scenario.get("headers") or {}).items()}
        changes: list[tuple[str, dict[str, Any]]] = []
        fields = serializers.get(route.get("serializer"), {}).get("fields", [])
        readonly = {
            f["name"]: {"sanka_read_only_probe": True}
            for f in fields
            if isinstance(f, dict) and f.get("read_only") and isinstance(f.get("name"), str)
        }
        if (
            readonly
            and method in {"POST", "PUT", "PATCH"}
            and isinstance(scenario.get("body"), dict)
        ):
            changes.append(("read-only", {"body": {**scenario["body"], **readonly}}))
        if method in {"POST", "PUT", "PATCH"}:
            changes.extend(_write_probes(scenario, request_only=request_only))
        authenticators = [
            name for name in (route.get("authentication") or []) if isinstance(name, str)
        ]
        if headers.get("authorization", "").startswith("Token ") and any(
            "TokenAuthentication" in name for name in authenticators
        ):
            changes.append(
                ("credential-rejection", {"headers": {**headers, "authorization": "Token"}})
            )
        if (
            method in {"POST", "PUT", "PATCH", "DELETE"}
            and "x-csrftoken" in headers
            and any(name.endswith("SessionAuthentication") for name in authenticators)
        ):
            changes.append(
                (
                    "csrf-rejection",
                    {"headers": {k: v for k, v in headers.items() if k != "x-csrftoken"}},
                )
            )
        group = []
        for kind, change in changes:
            probe = copy.deepcopy(dict(scenario))
            probe.pop("expected_source_status", None)
            probe.update(
                change,
                id=f"edge:{kind}:{scenario['id']}",
                probe_kind=kind,
                generated_from=route["path"],
                context_from=scenario["id"],
            )
            group.append(probe)
        groups.append(group)
    seen: set[tuple[str, str, str]] = set()
    # Share the existing budget across requests instead of exhausting it on the first body.
    for batch in zip_longest(*groups):
        for probe in batch:
            if probe is None:
                continue
            key = (probe["method"], probe["generated_from"], probe["probe_kind"])
            if key in seen:
                continue
            seen.add(key)
            probes.append(probe)
            if len(probes) == 12:
                return probes
    return probes


def _write_probes(
    scenario: Mapping[str, Any], *, request_only: bool = False
) -> list[tuple[str, dict[str, Any]]]:
    changes: list[tuple[str, dict[str, Any]]] = []
    multipart = scenario.get("multipart")
    if isinstance(multipart, dict):
        files = multipart.get("files")
        if isinstance(files, list) and files and isinstance(files[0], dict):
            boundary = str(multipart.get("boundary") or _MULTIPART_BOUNDARY)
            for kind, content in (
                ("upload-binary", b"\x00\xff\r\n sample\t\r\n"),
                ("upload-boundary", b"sample--" + boundary.encode() + b"-fragment\r\n"),
            ):
                mutated = copy.deepcopy(multipart)
                mutated["files"][0]["content_b64"] = base64.b64encode(content).decode()
                changes.append((kind, {"multipart": mutated}))
    body = scenario.get("body")
    if isinstance(body, dict):
        if request_only:
            for name, value in body.items():
                variants: list[tuple[str, Any]] = []
                if isinstance(value, list):
                    variants = [("null-collection", None)]
                elif isinstance(value, str | int | float | bool):
                    variants = [("null-scalar", None)]
                    if isinstance(value, str):
                        variants.extend(
                            [
                                ("blank-text", ""),
                                ("whitespace-text", " \t "),
                                ("padded-text", f" {value} "),
                            ]
                        )
                for kind, replacement in variants:
                    changes.append(
                        (f"{kind}:{name}", {"body": {**copy.deepcopy(body), name: replacement}})
                    )
                    if len(changes) == 12:
                        return changes
        # ponytail: two collection depths cover common nested writes; deeper graphs stay explicit.
        for depth in (0, 1):
            mutated = copy.deepcopy(body)
            if _duplicate_record(mutated, depth):
                changes.append((f"duplicate-record-{depth}", {"body": mutated}))
    return changes


def _duplicate_record(value: object, depth: int) -> bool:
    if isinstance(value, dict):
        for item in value.values():
            if isinstance(item, list) and item and isinstance(item[-1], dict):
                if depth == 0:
                    item.append(copy.deepcopy(item[-1]))
                    return True
                if _duplicate_record(item[-1], depth - 1):
                    return True
    return False


def _probe_context(route: str, scenarios: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    route_parts = route.split("/")
    matching = []
    for scenario in scenarios:
        parts = str(scenario.get("path", "")).split("?", 1)[0].split("/")
        if len(parts) == len(route_parts) and all(
            wanted == actual or (wanted.startswith("{") and wanted.endswith("}") and actual)
            for wanted, actual in zip(route_parts, parts, strict=True)
        ):
            matching.append(scenario)
    # ponytail: one context per route; use explicit scenarios for other auth/tenant variants.
    return min(
        matching,
        key=lambda item: (
            item.get("expected_source_status") not in range(200, 400),
            not bool(item.get("headers")),
        ),
        default=None,
    )


def _concrete_path(path: str) -> str | None:
    if "{" not in path:
        return path
    segments = path.split("/")
    substituted = 0
    for index, segment in enumerate(segments):
        if segment.startswith("{") and segment.endswith("}"):
            name = segment[1:-1].split(":", 1)[0]
            if name not in {"pk", "id"} and not name.endswith("_id") and not name.endswith("_pk"):
                return None
            segments[index] = "999999"
            substituted += 1
    return "/".join(segments) if substituted == 1 else None


def _edge(
    kind: str,
    method: str,
    path: str,
    source_path: str,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    context = context or {}
    return {
        "id": f"edge:{kind}:{method} {path}",
        "method": method,
        "path": path,
        "headers": dict(context.get("headers") or {}),
        "multipart": None,
        "capture_headers": list(
            dict.fromkeys([*EDGE_HEADERS, *context.get("capture_headers", [])])
        ),
        "response_body": None,
        "setup": copy.deepcopy(context.get("setup") or []),
        "generated_from": source_path,
        **({"context_from": context["id"]} if context.get("id") else {}),
    }


# ---------------------------------------------------------------------------
# Side processes


_POSTGRES_SCRIPT = r"""
import base64, datetime, decimal, json, math, sys, uuid
from urllib.parse import quote, urlsplit
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

payload = json.load(sys.stdin)
params = conninfo_to_dict(payload["admin"])
# Require a self-contained TCP URL: no libpq service/password-file/environment fallback.
if (
    not payload["admin"].startswith(("postgresql://", "postgres://"))
    or not params.get("host")
    or not params.get("user")
    or not params.get("password")
    or not params.get("dbname")
    or set(params) - {"host", "port", "user", "password", "dbname", "sslmode"}
):
    raise SystemExit("dedicated PostgreSQL URL required")
params.setdefault("port", "5432")
params["connect_timeout"] = "10"
action = payload["action"]
if action in {"create", "cleanup"}:
    with psycopg.connect(make_conninfo(**params), autocommit=True) as conn:
        if action == "create":
            if conn.execute("SELECT 1 FROM pg_database WHERE datname=%s",
                            (payload["name"],)).fetchone():
                print(json.dumps({"collision": True}))
                sys.exit(0)
            try:
                conn.execute(
                    sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                        sql.Identifier(payload["name"]),
                        sql.Identifier(payload.get("template") or "template0"),
                    )
                )
            except psycopg.errors.DuplicateDatabase:
                print(json.dumps({"collision": True}))
                sys.exit(0)
            conn.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                sql.Identifier(payload["name"]), sql.Literal(payload["ownership_token"])))
            auth = quote(params["user"], safe="")
            if params.get("password"):
                auth += ":" + quote(params["password"], safe="")
            host = params["host"]
            if ":" in host:
                host = "[" + host + "]"
            url = "postgresql://" + auth + "@" + host + ":" + params["port"] + "/" + payload["name"]
            if params.get("sslmode"):
                url += "?sslmode=" + quote(params["sslmode"], safe="")
            result = {"url": url}
        else:
            for name in payload["names"]:
                ownership = conn.execute(
                    "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
                    "WHERE datname = %s", (name,)).fetchone()
                if ownership is None:
                    continue
                if ownership[0] != payload["ownership_token"]:
                    print(json.dumps({"ownership_unconfirmed": True}))
                    sys.exit(0)
                conn.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (name,),
                )
                conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
            result = {"ok": True}
else:
    params["dbname"] = payload["name"]

    def typed(value):
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else {"float": str(value)}
        if isinstance(value, (bytes, memoryview)):
            return {"bytes": base64.b64encode(bytes(value)).decode()}
        if isinstance(value, dict):
            return {key: typed(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [typed(item) for item in value]
        return {type(value).__name__: str(value)}

    result = {}
    with psycopg.connect(make_conninfo(**params), autocommit=True) as conn:
        conn.execute("SET TIME ZONE 'UTC'")
        objects = conn.execute(
            "SELECT n.nspname, c.relname, c.relkind FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p','S') "
            "AND n.nspname NOT IN ('pg_catalog','information_schema') "
            "AND n.nspname NOT LIKE 'pg_toast%' ORDER BY 1,2"
        ).fetchall()
        for schema, name, kind in objects:
            key = schema + "." + name
            if name in payload["ignored"] or key in payload["ignored"]:
                continue
            ident = sql.Identifier(schema, name)
            if kind == "S":
                row = conn.execute(
                    sql.SQL("SELECT last_value, is_called FROM {}").format(ident)
                ).fetchone()
                result[key] = {
                    "columns": [["last_value", "int8"], ["is_called", "bool"]],
                    "rows": [list(row)],
                }
            else:
                columns = conn.execute(
                    "SELECT a.attname, format_type(a.atttypid, a.atttypmod) "
                    "FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname=%s AND c.relname=%s AND a.attnum>0 "
                    "AND NOT a.attisdropped ORDER BY a.attnum",
                    (schema, name),
                ).fetchall()
                rows = [
                    [typed(value) for value in row]
                    for row in conn.execute(sql.SQL("SELECT * FROM {}").format(ident)).fetchall()
                ]
                result[key] = {
                    "columns": columns,
                    "rows": sorted(rows, key=lambda row: json.dumps(row, sort_keys=True)),
                }
print(json.dumps(result, allow_nan=False))

"""


class _PostgresReplay:
    """Own only databases allocated by this invocation; drivers run in source Python."""

    def __init__(self, admin: str, python: Path, cwd: Path, env: Mapping[str, str]):
        self.admin, self.python, self.cwd, self.env = admin, python, cwd, env
        self.ownership_token = "sanka-replay-owner:" + os.urandom(32).hex()
        self.names: list[str] = []
        self.urls: dict[str, str] = {}

    def call(self, action: str, **values: Any) -> dict[str, Any]:
        try:
            return _run_side(
                _POSTGRES_SCRIPT,
                {
                    "side": "PostgreSQL " + action,
                    "database_backend": "postgresql",
                    "admin": self.admin,
                    "ownership_token": self.ownership_token,
                    "action": action,
                    **values,
                },
                python=self.python,
                cwd=self.cwd,
                env=self.env,
            )
        except (subprocess.TimeoutExpired, OSError):
            raise ReplayError(
                "PostgreSQL operation failed or timed out (details redacted)"
            ) from None

    def create(self, template: str | None = None) -> str:
        name = "sanka_replay_" + uuid.uuid4().hex
        self.names.append(name)  # Cleanup requires server-side ownership, even after timeout.
        result = self.call("create", name=name, template=self.urls[template] if template else None)
        if result.get("collision"):
            self.names.remove(name)
            raise ReplayError("PostgreSQL database name collision; existing database untouched")
        url = str(result["url"])
        self.urls[url] = name
        return url

    def snapshot(self, url: str, ignored: Iterable[str]) -> dict[str, dict[str, Any]]:
        return self.call("snapshot", name=self.urls[url], ignored=list(ignored))

    def cleanup(self) -> None:
        failures = 0
        unconfirmed = 0
        remaining: list[str] = []
        for name in reversed(self.names):
            try:
                result = self.call("cleanup", names=[name])
                if result.get("ownership_unconfirmed"):
                    unconfirmed += 1
                    remaining.append(name)
            except ReplayError:
                failures += 1
                remaining.append(name)
        if failures or unconfirmed:
            raise ReplayError(
                f"PostgreSQL cleanup failed for {failures + unconfirmed} isolated databases; "
                f"ownership unconfirmed for {unconfirmed}, left untouched for manual inspection; "
                f"database names: {', '.join(remaining)}"
            )


def _run_side(
    script: str, payload: Mapping[str, Any], *, python: Path, cwd: Path, env: Mapping[str, str]
) -> dict[str, Any]:
    try:
        outcome = subprocess.run(
            [str(python), "-c", script],
            cwd=cwd,
            env=dict(env),
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=_SIDE_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        if payload.get("database_backend") == "postgresql":
            raise ReplayError("PostgreSQL side failed or timed out (details redacted)") from None
        raise
    if outcome.returncode != 0:
        if payload.get("database_backend") == "postgresql":
            raise ReplayError(
                f"{payload.get('side', 'side')} PostgreSQL process failed (details redacted)"
            )
        detail = (outcome.stderr or outcome.stdout or "no output").strip()
        raise ReplayError(f"{payload.get('side', 'side')} process failed: {detail[-4000:]}")
    lines = [line for line in outcome.stdout.splitlines() if line.strip()]
    try:
        result = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError as error:
        raise ReplayError(f"{payload.get('side', 'side')} process returned invalid JSON") from error
    if not isinstance(result, dict):
        raise ReplayError(f"{payload.get('side', 'side')} process returned no result object")
    if (
        payload.get("side") == "prepare"
        and payload.get("seed")
        and result.get("failure_category") == "seed_failure"
    ):
        reason = (
            "Seed replaced the replay database."
            if result.get("database_replaced")
            else "Seed script failed."
        )
        detail = (
            ""
            if payload.get("database_backend") == "postgresql"
            else outcome.stderr.strip()[-4000:]
        )
        raise ReplayError(
            reason + " Replay already configures Django "
            "and runs migrations; seed existing tables without deleting or replacing the "
            "database, reconfiguring settings, or rerunning initialization."
            + ("\n" + detail if detail else ""),
            category="seed_failure",
        )
    return result


# ---------------------------------------------------------------------------
# Database snapshots and comparison


def snapshot_media(path: Path) -> dict[str, str]:
    """Compare relative names and bytes, never temporary directory names."""
    result = {}
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise ReplayError("media snapshots do not support symbolic links")
        if item.is_file():
            with item.open("rb") as stream:
                result[item.relative_to(path).as_posix()] = hashlib.file_digest(
                    stream, "sha256"
                ).hexdigest()
    return result


def snapshot_database(path: Path, ignored: Iterable[str]) -> dict[str, dict[str, Any]]:
    ignored_names = set(ignored)
    snapshot: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        raise ReplayError(f"isolated database is missing: {path}")
    connection = sqlite3.connect(str(path))
    try:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        for table in tables:
            if table in ignored_names or table.startswith("sqlite_"):
                continue
            cursor = connection.execute(f'SELECT * FROM "{table.replace(chr(34), chr(34) * 2)}"')
            columns = [str(item[0]) for item in cursor.description or ()]
            rows = sorted(
                ([_jsonable(value) for value in row] for row in cursor.fetchall()),
                key=lambda row: json.dumps(row, sort_keys=True),
            )
            snapshot[table] = {"columns": columns, "rows": rows}
    finally:
        connection.close()
    return snapshot


def _jsonable(value: object) -> Any:
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    return value


def diff_snapshots(
    source: Mapping[str, dict[str, Any]], candidate: Mapping[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    for table in sorted(set(source) | set(candidate)):
        left = source.get(table)
        right = candidate.get(table)
        if left is None or right is None:
            differences.append(
                {
                    "table": table,
                    "kind": "missing-table",
                    "source": left is not None,
                    "candidate": right is not None,
                }
            )
            continue
        if left["columns"] != right["columns"]:
            differences.append(
                {
                    "table": table,
                    "kind": "columns",
                    "source": left["columns"],
                    "candidate": right["columns"],
                }
            )
            continue
        if left["rows"] != right["rows"]:
            left_counts = Counter(json.dumps(row, sort_keys=True) for row in left["rows"])
            right_counts = Counter(json.dumps(row, sort_keys=True) for row in right["rows"])
            only_source = [json.loads(row) for row in (left_counts - right_counts).elements()]
            only_candidate = [json.loads(row) for row in (right_counts - left_counts).elements()]
            differences.append(
                {
                    "table": table,
                    "kind": "rows",
                    "source_rows": len(left["rows"]),
                    "candidate_rows": len(right["rows"]),
                    "only_in_source": only_source[:5],
                    "only_in_candidate": only_candidate[:5],
                }
            )
    return differences


def normalize_body(content: bytes, content_type: str, response_body: str | None) -> Any:
    if not content:
        return None
    if response_body == "base64":
        return {"base64": base64.b64encode(content).decode("ascii")}
    if content_type.split(";", 1)[0].strip() == "application/json" or content[:1] in (b"{", b"["):
        try:
            return json.loads(content)
        except (ValueError, json.JSONDecodeError):
            pass
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return {"base64": base64.b64encode(content).decode("ascii")}


def body_difference(source: Any, candidate: Any, path: str = "$") -> str | None:
    if isinstance(source, dict) and isinstance(candidate, dict):
        for key in sorted(set(source) | set(candidate)):
            if key not in source:
                return f"{path}.{key}: only in candidate"
            if key not in candidate:
                return f"{path}.{key}: only in source"
            inner = body_difference(source[key], candidate[key], f"{path}.{key}")
            if inner:
                return inner
    if isinstance(source, list) and isinstance(candidate, list):
        if len(source) != len(candidate):
            return f"{path}: {len(source)} items in source, {len(candidate)} in candidate"
        for index, (left, right) in enumerate(zip(source, candidate, strict=True)):
            inner = body_difference(left, right, f"{path}[{index}]")
            if inner:
                return inner
    if isinstance(source, (dict, list)) and isinstance(candidate, type(source)):
        return None
    if isinstance(source, bool) != isinstance(candidate, bool):
        return f"{path}: source={_short(source)} candidate={_short(candidate)}"
    if source == candidate:
        return None
    return f"{path}: source={_short(source)} candidate={_short(candidate)}"


def _short(value: Any) -> str:
    text = (
        json.dumps(value, ensure_ascii=False, sort_keys=True)
        if not isinstance(value, str)
        else value
    )
    return text if len(text) <= 160 else text[:157] + "..."


# ---------------------------------------------------------------------------
# Replay


def default_interpreter(root: Path, fallback: Path) -> Path:
    """Prefer the checkout's own virtualenv so its Django or FastAPI stack is importable.

    The extension runs from its own isolated environment, which deliberately carries
    no framework packages; the source and the candidate each bring theirs.
    """
    for relative in (("bin", "python"), ("Scripts", "python.exe")):
        candidate = root / ".venv" / Path(*relative)
        if candidate.is_file():
            return candidate
    return fallback


def replay(
    project_root: Path,
    scenarios: Sequence[Mapping[str, Any]],
    *,
    settings_module: str,
    candidate_root: Path | None = None,
    entrypoint: str = DEFAULT_ENTRYPOINT,
    db_env: str = DEFAULT_DB_ENV,
    candidate_db_env: str | None = None,
    seed: Path | None = None,
    ignored_tables: Iterable[str] = DEFAULT_IGNORED_TABLES,
    all_headers: bool = False,
    python: Path | None = None,
    candidate_python: Path | None = None,
    keep_temp: bool = False,
    target: str = "fastapi",
    database_backend: str = "sqlite",
    postgres_admin_dsn_env: str | None = None,
) -> dict[str, Any]:
    """Replay ``scenarios`` against the source and the candidate and return the report."""
    if database_backend not in {"sqlite", "postgresql"}:
        raise ReplayError("database_backend must be sqlite or postgresql")
    if database_backend == "postgresql" and not (
        postgres_admin_dsn_env and os.environ.get(postgres_admin_dsn_env)
    ):
        raise ReplayError(
            "PostgreSQL replay requires postgres_admin_dsn_env naming a dedicated test admin URL"
        )
    if database_backend == "sqlite" and postgres_admin_dsn_env:
        raise ReplayError("postgres_admin_dsn_env requires database_backend=postgresql")
    if target not in {"fastapi", "flask", "go"}:
        raise ReplayError("target must be fastapi, flask or go")
    if target == "go" and database_backend != "sqlite":
        raise ReplayError("Go candidate replay currently requires SQLite")
    if not scenarios:
        raise ReplayError("scenarios must not be empty")
    project = Path(project_root).resolve()
    candidate = Path(candidate_root).resolve() if candidate_root is not None else project
    source_python = (
        Path(python) if python is not None else default_interpreter(project, Path(sys.executable))
    )
    target_python = (
        Path(candidate_python)
        if candidate_python is not None
        else default_interpreter(candidate, source_python)
    )
    if not (candidate / entrypoint).is_file():
        raise ReplayError(
            f"candidate entrypoint not found: {candidate / entrypoint}",
            category="candidate_failure",
        )
    if seed is not None and not Path(seed).is_file():
        raise ReplayError(f"seed file not found: {seed}")
    target_db_env = candidate_db_env or db_env
    ignored = tuple(dict.fromkeys(ignored_tables))
    temp = Path(tempfile.mkdtemp(prefix="sanka-replay-"))
    base_environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"DJANGO_SETTINGS_MODULE", db_env, target_db_env, postgres_admin_dsn_env}
        and (
            database_backend != "postgresql"
            or (not key.startswith("PG") and key not in {"DATABASE_URL", "SANKA_DATABASE_URL"})
        )
    }
    postgres = (
        _PostgresReplay(
            os.environ[str(postgres_admin_dsn_env)], source_python, project, base_environment
        )
        if database_backend == "postgresql"
        else None
    )
    reports: list[dict[str, Any]] = []
    go_snapshot = None
    replay_scenarios = [dict(scenario) for scenario in scenarios]
    try:
        if target == "go":
            from .go import prepare_candidate

            candidate, go_snapshot = prepare_candidate(candidate, entrypoint, temp)
        base_db = postgres.create() if postgres else temp / "base.sqlite3"
        base_media = temp / "base-media"
        base_media.mkdir()
        _run_side(
            _PREPARE_SCRIPT,
            {
                "side": "prepare",
                "database_backend": database_backend,
                "project_root": str(project),
                "settings_module": settings_module,
                "db_env": db_env,
                "database": str(base_db),
                "media_root": str(base_media),
                "seed": str(Path(seed).resolve()) if seed is not None else None,
            },
            python=source_python,
            cwd=project,
            env=base_environment,
        )
        for index, scenario in enumerate(replay_scenarios):
            if scenario.get("probe_kind"):
                baseline = next((r for r in reports if r["id"] == scenario["context_from"]), None)
                # Keep the mutated credentials/body. Never replace them with a positive context.
                scenario["baseline_source_status"] = (
                    baseline["source"]["status"] if baseline else None
                )
            elif scenario.get("generated_from") and scenario.get("context_from"):
                original = next(
                    (
                        s
                        for s in replay_scenarios[:index]
                        if s.get("id") == scenario["context_from"]
                    ),
                    None,
                )
                # Reuse a context proven by the source, not the first credential-looking header.
                # Stay on the same concrete resource; keep all supplied negative tests intact.
                successful = [
                    {**s, "expected_source_status": r["source"]["status"]}
                    for s, r in zip(replay_scenarios[:index], reports, strict=True)
                    if original is not None
                    and not s.get("generated_from")
                    and s.get("path") == original.get("path")
                    and 200 <= r["source"]["status"] < 400
                ]
                context = _probe_context(scenario["generated_from"], successful)
                if context is not None:
                    scenario.update(
                        headers=dict(context.get("headers") or {}),
                        setup=copy.deepcopy(context.get("setup") or []),
                        context_from=context.get("id"),
                    )
            reports.append(
                _replay_one(
                    scenario,
                    target=target,
                    index=index,
                    project=project,
                    candidate=candidate,
                    entrypoint=entrypoint,
                    settings_module=settings_module,
                    db_env=db_env,
                    candidate_db_env=target_db_env,
                    base_db=base_db,
                    base_media=base_media,
                    temp=temp,
                    ignored=ignored,
                    all_headers=all_headers,
                    source_python=source_python,
                    target_python=target_python,
                    environment=base_environment,
                    postgres=postgres,
                )
            )
    finally:
        try:
            if postgres:
                postgres.cleanup()
        finally:
            if not keep_temp:
                shutil.rmtree(temp, ignore_errors=True)
    if go_snapshot is not None:
        from .go import snapshot

        if snapshot(Path(candidate_root or project)) != go_snapshot:
            raise ReplayError("candidate changed during verification")
    matched = [report for report in reports if report["match"]]
    summary = {
        "scenarios": len(reports),
        "matched": len(matched),
        "mismatched": len(reports) - len(matched),
        "status_mismatches": sum(1 for report in reports if not report["status_match"]),
        "source_expectation_mismatches": sum(
            1 for report in reports if not report.get("source_expectation_match", True)
        ),
        "body_mismatches": sum(1 for report in reports if not report["body_match"]),
        "header_mismatches": sum(1 for report in reports if not report["headers_match"]),
        "database_mismatches": sum(1 for report in reports if not report["database_match"]),
        "media_mismatches": sum(1 for report in reports if not report["media_match"]),
        "non_native": sum(1 for report in reports if not report["native"]["compliant"]),
        "generated_probes": sum(1 for report in reports if report.get("generated_from")),
        "source_statuses": dict(
            sorted(Counter(str(report["source"]["status"]) for report in reports).items())
        ),
    }
    coverage_issues: list[dict[str, Any]] = []
    warnings = []
    source_expectations = [r["id"] for r in reports if not r.get("source_expectation_match", True)]
    if source_expectations:
        coverage_issues.append(
            {"code": "source_expectation_mismatch", "scenario_ids": source_expectations}
        )
        warnings.append(
            "Source responses did not satisfy expected_source_status. Check seed data, "
            "authentication and scenario prerequisites before repairing the candidate."
        )
    if all(report["source"]["status"] >= 400 for report in reports):
        coverage_issues.append(
            {"code": "missing_success_coverage", "scenario_ids": [r["id"] for r in reports]}
        )
        warnings.append(
            "Only source error responses were exercised. Use --seed and expected_source_status "
            "for intended success paths; matching errors do not establish completeness."
        )
    successful_paths = {r.get("path") for r in reports if 200 <= r["source"]["status"] < 400}
    # Compare request context, not only status/path: a declared anonymous rejection
    # cannot excuse a generated authenticated probe failing to reach its handler.
    context_fields = ("method", "path", "headers", "setup", "body", "body_base64", "multipart")
    intentional_auth = [
        (r["source"]["status"], {key: scenario.get(key) for key in context_fields})
        for scenario, r in zip(replay_scenarios, reports, strict=True)
        if not r.get("generated_from")
        and r.get("expected_source_status") == r["source"]["status"]
        and r["source"]["status"] in {401, 403}
        and r.get("path") in successful_paths
    ]
    blocked = [
        r["id"]
        for scenario, r in zip(replay_scenarios, reports, strict=True)
        if r.get("generated_from")
        and r["source"]["status"] in {401, 403}
        and not (
            r.get("probe_kind") in {"credential-rejection", "csrf-rejection"}
            and r.get("baseline_source_status") in range(200, 400)
        )
        and (r["source"]["status"], {key: scenario.get(key) for key in context_fields})
        not in intentional_auth
    ]
    if blocked:
        coverage_issues.append({"code": "authentication_coverage", "scenario_ids": blocked})
        warnings.append(
            "Generated probes stopped at authentication or authorization. Add authenticated "
            "scenarios to exercise the intended handlers, including OPTIONS."
        )
    return {
        "schema": REPLAY_SCHEMA,
        **(
            {
                "candidate_digest": hashlib.sha256(
                    json.dumps(go_snapshot, sort_keys=True).encode()
                ).hexdigest(),
                "verification_scope": (
                    "public HTTP and SQLite parity; compiled Go; not full native compliance"
                ),
            }
            if go_snapshot is not None
            else {}
        ),
        "ok": summary["mismatched"] == 0 and summary["non_native"] == 0,
        "project_root": str(project),
        "candidate_root": str(Path(candidate_root or project).resolve()),
        "entrypoint": entrypoint,
        "settings_module": settings_module,
        "database": {
            "backend": database_backend,
            "isolation_env": db_env,
            "candidate_isolation_env": target_db_env,
            "ignored_tables": list(ignored),
            "seed": str(seed) if seed else None,
        },
        "headers": "all" if all_headers else "declared",
        "summary": summary,
        "warnings": warnings,
        "coverage_issues": coverage_issues,
        "scenarios": reports,
        "summary_lines": _summary_lines(summary, reports),
    }


def _replay_one(
    scenario: Mapping[str, Any],
    *,
    index: int,
    target: str,
    project: Path,
    candidate: Path,
    entrypoint: str,
    settings_module: str,
    db_env: str,
    candidate_db_env: str,
    base_db: Path | str,
    base_media: Path,
    temp: Path,
    ignored: tuple[str, ...],
    all_headers: bool,
    source_python: Path,
    target_python: Path,
    environment: Mapping[str, str],
    postgres: _PostgresReplay | None = None,
) -> dict[str, Any]:
    identifier = str(scenario.get("id") or f"scenario-{index}")
    source_db: Path | str
    candidate_db: Path | str
    if postgres:
        source_db = postgres.create(str(base_db))
        candidate_db = postgres.create(str(base_db))
        source_before = postgres.snapshot(source_db, ignored)
        candidate_before = postgres.snapshot(candidate_db, ignored)
        if source_before != candidate_before:
            raise ReplayError("PostgreSQL scenario clones do not have identical seed state")
    else:
        source_db = temp / f"source-{index}.sqlite3"
        candidate_db = temp / f"candidate-{index}.sqlite3"
        shutil.copy2(base_db, source_db)
        shutil.copy2(base_db, candidate_db)
        source_before = snapshot_database(Path(base_db), ignored)
        candidate_before = source_before
    before_counts = {table: len(data["rows"]) for table, data in source_before.items()}
    source_media = temp / f"source-{index}-media"
    candidate_media = temp / f"candidate-{index}-media"
    shutil.copytree(base_media, source_media)
    shutil.copytree(base_media, candidate_media)
    request = {
        "method": scenario["method"],
        "path": scenario["path"],
        "headers": dict(scenario.get("headers") or {}),
        **({"body": scenario["body"]} if "body" in scenario else {}),
        **({"body_base64": scenario["body_base64"]} if "body_base64" in scenario else {}),
        "multipart": scenario.get("multipart"),
    }
    setup = [
        {
            "method": step["method"],
            "path": step["path"],
            "headers": dict(step.get("headers") or {}),
            **({"body": step["body"]} if "body" in step else {}),
            **({"body_base64": step["body_base64"]} if "body_base64" in step else {}),
            "multipart": step.get("multipart"),
        }
        for step in scenario.get("setup") or []
    ]
    common = {
        "database_backend": "postgresql" if postgres else "sqlite",
        "request": request,
        "setup": setup,
        "db_env": db_env,
        "boundary": _MULTIPART_BOUNDARY,
    }
    source_result = _run_side(
        _SOURCE_SCRIPT,
        {
            **common,
            "side": f"source[{identifier}]",
            "project_root": str(project),
            "settings_module": settings_module,
            "database": str(source_db),
            "media_root": str(source_media),
        },
        python=source_python,
        cwd=project,
        env=environment,
    )
    if target == "go":
        from .go import run_candidate

        candidate_result = run_candidate(
            candidate, request, setup, str(candidate_db), candidate_db_env, candidate_media
        )
    else:
        candidate_result = _run_side(
            _CANDIDATE_SCRIPT,
            {
                **common,
                "side": f"candidate[{identifier}]",
                "project_root": str(project),
                "candidate_root": str(candidate),
                "candidate_db_env": candidate_db_env,
                "target": target,
                "entrypoint": entrypoint,
                "database": str(candidate_db),
                "settings_module": settings_module,
                "media_root": str(candidate_media),
            },
            python=target_python,
            cwd=candidate,
            env=environment,
        )
    response_body = scenario.get("response_body")
    source_bytes = base64.b64decode(source_result["body_b64"])
    candidate_bytes = base64.b64decode(candidate_result["body_b64"])
    source_headers: dict[str, str] = dict(source_result.get("headers") or {})
    candidate_headers: dict[str, str] = dict(candidate_result.get("headers") or {})
    source_body = normalize_body(
        source_bytes, source_headers.get("content-type", ""), response_body
    )
    candidate_body = normalize_body(
        candidate_bytes, candidate_headers.get("content-type", ""), response_body
    )
    if all_headers:
        compared = sorted((set(source_headers) | set(candidate_headers)) - VOLATILE_HEADERS)
    else:
        compared = [str(name).lower() for name in scenario.get("capture_headers") or []]
    header_diffs = {
        name: {"source": source_headers.get(name, ""), "candidate": candidate_headers.get(name, "")}
        for name in compared
        if source_headers.get(name, "") != candidate_headers.get(name, "")
    }
    source_snapshot = (
        postgres.snapshot(str(source_db), ignored)
        if postgres
        else snapshot_database(Path(source_db), ignored)
    )
    candidate_snapshot = (
        postgres.snapshot(str(candidate_db), ignored)
        if postgres
        else snapshot_database(Path(candidate_db), ignored)
    )
    database_diffs = diff_snapshots(source_snapshot, candidate_snapshot)
    native = dict(candidate_result.get("native") or {})
    native_compliant = bool(native.get("is_apiroute")) and bool(native.get("endpoint_in_candidate"))
    status_match = int(source_result["status"]) == int(candidate_result["status"])
    expected_source_status = scenario.get("expected_source_status")
    source_expectation_match = (
        expected_source_status is None or int(source_result["status"]) == expected_source_status
    )
    if target == "go":
        native_compliant = bool(native.get("compiled_go"))
    if target == "flask":
        native_compliant = (
            bool(native.get("is_flask"))
            and bool(native.get("default_wsgi_dispatch"))
            and not native.get("forbidden_imports")
            and (
                bool(native.get("endpoint_in_candidate")) or bool(native.get("framework_response"))
            )
        )
    body_match = (
        bool(source_bytes) == bool(candidate_bytes)
        and body_difference(source_body, candidate_body) is None
    )
    headers_match = not header_diffs
    database_match = not database_diffs
    source_files = snapshot_media(source_media)
    candidate_files = snapshot_media(candidate_media)
    media_match = source_files == candidate_files
    report: dict[str, Any] = {
        "id": identifier,
        "method": scenario["method"],
        "path": scenario["path"],
        "match": source_expectation_match
        and status_match
        and body_match
        and headers_match
        and database_match
        and media_match,
        "expected_source_status": expected_source_status,
        "source_expectation_match": source_expectation_match,
        "status_match": status_match,
        "body_match": body_match,
        "headers_match": headers_match,
        "database_match": database_match,
        "media_match": media_match,
        "media_differences": [
            name
            for name in sorted(source_files.keys() | candidate_files.keys())
            if source_files.get(name) != candidate_files.get(name)
        ],
        "source": {
            "status": source_result["status"],
            "database_before": before_counts,
            "database_after": {table: len(data["rows"]) for table, data in source_snapshot.items()},
            "headers": {name: source_headers.get(name, "") for name in compared},
        },
        "candidate": {
            "status": candidate_result["status"],
            "database_before": before_counts,
            "database_after": {
                table: len(data["rows"]) for table, data in candidate_snapshot.items()
            },
            "headers": {name: candidate_headers.get(name, "") for name in compared},
        },
        "body_difference": None
        if body_match
        else (
            "empty response vs non-empty response"
            if bool(source_bytes) != bool(candidate_bytes)
            else body_difference(source_body, candidate_body)
        ),
        "header_differences": header_diffs,
        "database_differences": database_diffs,
        "native": {**native, "compliant": native_compliant},
    }
    multipart_spec = scenario.get("multipart")
    if isinstance(multipart_spec, dict) and not report["match"]:
        delimiter = b"--" + str(multipart_spec.get("boundary") or _MULTIPART_BOUNDARY).encode()
        if any(
            delimiter in base64.b64decode(item.get("content_b64", ""))
            for item in multipart_spec.get("files", [])
        ):
            report["mismatch_kind"] = "multipart_boundary_parity"
            report["repair_hint"] = (
                "The uploaded bytes contain the multipart boundary token. Compare source and "
                "candidate parsing before changing validation: source compatibility may include "
                "legacy truncation, not full file preservation. "
                + (
                    "Reuse generated sanka_form.parse_form(request), then rerun verify."
                    if target == "flask"
                    else "Preserve the source parser behavior, then rerun verify."
                )
            )
            report["media_sizes"] = [
                {
                    "path": name,
                    "source": (source_media / name).stat().st_size
                    if name in source_files
                    else None,
                    "candidate": (candidate_media / name).stat().st_size
                    if name in candidate_files
                    else None,
                }
                for name in report["media_differences"][:5]
            ]
    if scenario.get("generated_from"):
        report["generated_from"] = scenario["generated_from"]
    if scenario.get("context_from"):
        report["context_from"] = scenario["context_from"]
    if scenario.get("probe_kind"):
        report["probe_kind"] = scenario["probe_kind"]
        report["baseline_source_status"] = scenario.get("baseline_source_status")
    return report


def _summary_lines(summary: Mapping[str, Any], reports: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = [
        f"{summary['matched']}/{summary['scenarios']} scenarios match "
        f"(status {summary['status_mismatches']}, body {summary['body_mismatches']}, "
        f"headers {summary['header_mismatches']}, "
        f"database {summary['database_mismatches']}, "
        f"media {summary['media_mismatches']} mismatches; "
        f"source expectations {summary.get('source_expectation_mismatches', 0)}; "
        f"{summary['non_native']} without native candidate routing)"
    ]
    for report in reports:
        if report["match"] and report["native"]["compliant"]:
            continue
        problems: list[str] = []
        if not report.get("source_expectation_match", True):
            problems.append(
                f"source expected {report['expected_source_status']}, "
                f"got {report['source']['status']}; "
                "check fixtures/authentication before changing the candidate"
            )
        if not report["status_match"]:
            problems.append(
                f"status {report['source']['status']} vs {report['candidate']['status']}"
            )
        if not report["body_match"]:
            problems.append(f"body {report['body_difference']}")
        if not report["headers_match"]:
            problems.append(
                "headers "
                + ", ".join(
                    f"{name}: {values['source']!r} vs {values['candidate']!r}"
                    for name, values in report["header_differences"].items()
                )
            )
        if not report["database_match"]:
            problems.append(
                "database "
                + ", ".join(
                    f"{item['table']} ({item['kind']})" for item in report["database_differences"]
                )
            )
        if not report["media_match"]:
            names = report["media_differences"]
            problems.append(
                "media "
                + ", ".join(names[:8])
                + (f" (+{len(names) - 8} more)" if len(names) > 8 else "")
            )
        if not report["native"]["compliant"]:
            problems.append(
                f"served by {report['native'].get('route_class')} "
                f"(endpoint in candidate: {report['native'].get('endpoint_in_candidate')})"
            )
        lines.append(
            f"{report['id']} [{report['method']} {report['path']}]: " + "; ".join(problems)
        )
    return lines


def save_report(report: Mapping[str, Any], artifact_root: Path) -> dict[str, Any]:
    """Persist all scenarios; keep protocol responses bounded and useful for repairs."""
    artifact_root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".json",
        prefix="replay-",
        dir=artifact_root,
        delete=False,
    ) as handle:
        json.dump(report, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        path = Path(handle.name).resolve()
    failures = [
        item for item in report["scenarios"] if not item["match"] or not item["native"]["compliant"]
    ]
    return {
        "schema": report["schema"],
        "ok": report["ok"],
        "summary": report["summary"],
        "warnings": report.get("warnings", []),
        "coverage_issues": report.get("coverage_issues", []),
        "report_path": str(path),
        "failures": [
            {
                "id": item["id"],
                "message": line[:2000],
                **{
                    key: item[key]
                    for key in ("mismatch_kind", "repair_hint", "media_sizes")
                    if key in item
                },
            }
            for item, line in zip(failures[:20], report["summary_lines"][1:21], strict=True)
        ],
        "omitted_failures": max(0, len(failures) - 20),
    }

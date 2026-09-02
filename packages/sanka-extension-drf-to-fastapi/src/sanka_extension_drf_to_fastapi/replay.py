# SPDX-License-Identifier: Apache-2.0
"""Differential scenario replay: the source Django application against a candidate.

The candidate is any FastAPI application exposing ``app`` from an entrypoint module.
Every scenario starts from an identical freshly migrated (and optionally seeded)
SQLite database, is sent to both applications, and the responses and the resulting
database state are compared. Nothing here depends on a Sanka plan or a generated
manifest, so the replay works at any readiness, including zero.

Request and response semantics deliberately match the Sanka Migration Bench
evaluator: the source side uses Django's test client with CSRF enforcement and a
JSON content type, the candidate side uses FastAPI's ``TestClient`` without
following redirects, declared headers are compared lower-cased, bodies compare as
JSON when both parse and as bytes otherwise, and multipart bodies are encoded with
the bench's fixed boundary.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

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
    multipart = item.get("multipart")
    if multipart is not None and not isinstance(multipart, dict):
        raise ReplayError(f"{label}.multipart must be an object")
    capture = item.get("capture_headers") or []
    if not isinstance(capture, list) or not all(isinstance(name, str) for name in capture):
        raise ReplayError(f"{label}.capture_headers must be an array of header names")
    response_body = item.get("response_body")
    if response_body not in (None, "base64"):
        raise ReplayError(f"{label}.response_body must be omitted or 'base64'")
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
        "headers": dict(headers),
        "body": item.get("body"),
        "multipart": multipart,
        "capture_headers": [str(name).lower() for name in capture],
        "response_body": response_body,
        "setup": setup,
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


def edge_probes_from_scan(scan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Derive read-only edge scenarios from a scan artifact's route list.

    Per concrete path: OPTIONS (Allow), one method the source does not declare
    (405 and Allow), the trailing-slash variant (redirect or 404 with Location),
    and, for single-parameter detail paths, a request for an object that does not
    exist (404 body). None of these mutate state.
    """
    routes = scan.get("routes")
    if not isinstance(routes, list):
        return []
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
        concrete = _concrete_path(path)
        if concrete is None:
            continue
        probes.append(_edge("options", "OPTIONS", concrete, path))
        unsupported = next(
            (
                candidate
                for candidate in ("TRACE", "PATCH", "PUT", "DELETE", "POST")
                if candidate not in methods
            ),
            None,
        )
        if unsupported is not None:
            probes.append(_edge("method-not-allowed", unsupported, concrete, path))
        variant = concrete[:-1] if concrete.endswith("/") and len(concrete) > 1 else concrete + "/"
        probes.append(_edge("slash-variant", "GET", variant, path))
        if "{" in path:
            probes.append(_edge("missing-object", "GET", concrete, path))
    return probes


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


def _edge(kind: str, method: str, path: str, source_path: str) -> dict[str, Any]:
    return {
        "id": f"edge:{kind}:{method} {path}",
        "method": method,
        "path": path,
        "headers": {},
        "body": None,
        "multipart": None,
        "capture_headers": list(EDGE_HEADERS),
        "response_body": None,
        "setup": [],
        "generated_from": source_path,
    }


# ---------------------------------------------------------------------------
# Side processes


_PREPARE_SCRIPT = r"""
import json, os, runpy, sys
payload = json.load(sys.stdin)
sys.path.insert(0, payload["project_root"])
os.environ["DJANGO_SETTINGS_MODULE"] = payload["settings_module"]
os.environ[payload["db_env"]] = payload["database"]
import django
django.setup()
from django.core.management import call_command
call_command("migrate", interactive=False, verbosity=0, run_syncdb=True)
if payload.get("seed"):
    runpy.run_path(payload["seed"], run_name="__main__")
from django.db import connections
connections.close_all()
print(json.dumps({"ok": True}))
"""

_SOURCE_SCRIPT = r"""
import base64, json, os, sys
payload = json.load(sys.stdin)
sys.path.insert(0, payload["project_root"])
os.environ["DJANGO_SETTINGS_MODULE"] = payload["settings_module"]
os.environ[payload["db_env"]] = payload["database"]
import django
django.setup()
from django.test import Client
client = Client(enforce_csrf_checks=True)

def multipart(spec):
    boundary = str(spec.get("boundary") or payload["boundary"]).encode("ascii")
    chunks = []
    for name, value in (spec.get("fields") or {}).items():
        disposition = 'Content-Disposition: form-data; name="%s"' % name
        chunks += [b"--" + boundary, disposition.encode(), b"", str(value).encode()]
    for item in spec.get("files") or []:
        disposition = 'Content-Disposition: form-data; name="%s"; filename="%s"' % (
            item["field"], item["filename"])
        content_type = item.get("content_type") or "application/octet-stream"
        chunks += [b"--" + boundary, disposition.encode(),
                   ("Content-Type: %s" % content_type).encode("ascii"), b"",
                   base64.b64decode(str(item["content_b64"]), validate=True)]
    chunks += [b"--" + boundary + b"--", b""]
    return b"\r\n".join(chunks), boundary.decode("ascii")

def send(request):
    headers = {"HTTP_" + key.upper().replace("-", "_"): value
               for key, value in request.get("headers", {}).items()}
    if request.get("multipart") is not None:
        body, boundary = multipart(request["multipart"])
        response = client.generic(
            request["method"], request["path"], data=body,
            content_type="multipart/form-data; boundary=" + boundary, **headers)
    else:
        body = request.get("body")
        response = client.generic(
            request["method"], request["path"],
            data=json.dumps(body) if body is not None else "",
            content_type="application/json", **headers)
    if getattr(response, "streaming", False):
        content = b"".join(response.streaming_content)
    else:
        content = bytes(response.content)
    return {"status": response.status_code,
            "headers": {str(key).lower(): str(value) for key, value in response.headers.items()},
            "body_b64": base64.b64encode(content).decode("ascii")}

for step in payload["setup"]:
    send(step)
result = send(payload["request"])
from django.db import connections
connections.close_all()
print(json.dumps(result))
"""

_CANDIDATE_SCRIPT = r"""
import base64, importlib.util, inspect, json, os, sys
payload = json.load(sys.stdin)
candidate_root = payload["candidate_root"]
for entry in (candidate_root, payload["project_root"]):
    if entry not in sys.path:
        sys.path.insert(0, entry)
os.environ[payload["db_env"]] = payload["database"]
entrypoint_path = os.path.join(candidate_root, payload["entrypoint"])
spec = importlib.util.spec_from_file_location("_sanka_replay_candidate", entrypoint_path)
module = importlib.util.module_from_spec(spec)
sys.modules["_sanka_replay_candidate"] = module
spec.loader.exec_module(module)
app = getattr(module, "app", None)
if app is None:
    raise SystemExit("candidate entrypoint does not expose `app`")
from fastapi.testclient import TestClient
from starlette.routing import Match

def resolve(route, scope, depth=0):
    if route is None or depth > 16 or type(route).__qualname__ != "_IncludedRouter":
        return route
    for candidate in route.effective_candidates():
        try:
            match, _ = candidate.matches(scope)
        except Exception:
            continue
        if match == Match.FULL:
            inner = resolve(candidate, scope, depth + 1)
            return getattr(inner, "original_route", inner)
    return route

def native(method, path):
    scope = {"type": "http", "method": method, "path": path.split("?", 1)[0],
             "root_path": "", "headers": [], "query_string": b""}
    matched = None
    for route in getattr(app, "routes", []):
        match, _ = route.matches(scope)
        if match == Match.FULL:
            matched = resolve(route, scope)
            break
    if matched is None:
        return {"route_class": None, "is_apiroute": False,
                "endpoint_in_candidate": False}
    cls = type(matched)
    try:
        from fastapi.routing import APIRoute
        is_apiroute = isinstance(matched, APIRoute)
    except Exception:
        is_apiroute = False
    endpoint = getattr(matched, "endpoint", None)
    filename = None
    try:
        filename = (inspect.getsourcefile(inspect.unwrap(endpoint))
                    if endpoint is not None else None)
    except Exception:
        filename = None
    root = os.path.realpath(candidate_root) + os.sep
    inside = bool(filename) and os.path.realpath(filename).startswith(root)
    return {"route_class": cls.__module__ + "." + cls.__qualname__,
            "is_apiroute": is_apiroute, "endpoint_in_candidate": inside}

def multipart(spec):
    boundary = str(spec.get("boundary") or payload["boundary"]).encode("ascii")
    chunks = []
    for name, value in (spec.get("fields") or {}).items():
        disposition = 'Content-Disposition: form-data; name="%s"' % name
        chunks += [b"--" + boundary, disposition.encode(), b"", str(value).encode()]
    for item in spec.get("files") or []:
        disposition = 'Content-Disposition: form-data; name="%s"; filename="%s"' % (
            item["field"], item["filename"])
        content_type = item.get("content_type") or "application/octet-stream"
        chunks += [b"--" + boundary, disposition.encode(),
                   ("Content-Type: %s" % content_type).encode("ascii"), b"",
                   base64.b64decode(str(item["content_b64"]), validate=True)]
    chunks += [b"--" + boundary + b"--", b""]
    return b"\r\n".join(chunks), boundary.decode("ascii")

with TestClient(app, follow_redirects=False) as client:
    def send(request):
        headers = dict(request.get("headers", {}))
        if request.get("multipart") is not None:
            body, boundary = multipart(request["multipart"])
            headers.setdefault("content-type", "multipart/form-data; boundary=" + boundary)
            response = client.request(request["method"], request["path"],
                                      content=body, headers=headers)
        else:
            response = client.request(request["method"], request["path"],
                                      json=request.get("body"), headers=headers)
        return {"status": response.status_code,
                "headers": {str(key).lower(): str(value)
                            for key, value in response.headers.items()},
                "body_b64": base64.b64encode(response.content).decode("ascii")}
    for step in payload["setup"]:
        send(step)
    result = send(payload["request"])
result["native"] = native(payload["request"]["method"], payload["request"]["path"])
print(json.dumps(result))
"""


def _run_side(
    script: str, payload: Mapping[str, Any], *, python: Path, cwd: Path, env: Mapping[str, str]
) -> dict[str, Any]:
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
    if outcome.returncode != 0:
        detail = (outcome.stderr or outcome.stdout or "no output").strip()
        raise ReplayError(f"{payload.get('side', 'side')} process failed: {detail[-4000:]}")
    lines = [line for line in outcome.stdout.splitlines() if line.strip()]
    try:
        result = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError as error:
        raise ReplayError(f"{payload.get('side', 'side')} process returned invalid JSON") from error
    if not isinstance(result, dict):
        raise ReplayError(f"{payload.get('side', 'side')} process returned no result object")
    return result


# ---------------------------------------------------------------------------
# Database snapshots and comparison


def snapshot_database(path: Path, ignored: Iterable[str]) -> dict[str, dict[str, Any]]:
    ignored_names = set(ignored)
    snapshot: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return snapshot
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
            cursor = connection.execute(f'SELECT * FROM "{table}"')
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
            only_source = [row for row in left["rows"] if row not in right["rows"]]
            only_candidate = [row for row in right["rows"] if row not in left["rows"]]
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
    if source == candidate:
        return None
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
    neither Django nor FastAPI; the source and the candidate each bring theirs.
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
    seed: Path | None = None,
    ignored_tables: Iterable[str] = DEFAULT_IGNORED_TABLES,
    all_headers: bool = False,
    python: Path | None = None,
    candidate_python: Path | None = None,
    keep_temp: bool = False,
) -> dict[str, Any]:
    """Replay ``scenarios`` against the source and the candidate and return the report."""
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
        raise ReplayError(f"candidate entrypoint not found: {candidate / entrypoint}")
    if seed is not None and not Path(seed).is_file():
        raise ReplayError(f"seed file not found: {seed}")
    ignored = tuple(dict.fromkeys(ignored_tables))
    temp = Path(tempfile.mkdtemp(prefix="sanka-replay-"))
    base_environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"DJANGO_SETTINGS_MODULE", db_env}
    }
    reports: list[dict[str, Any]] = []
    try:
        base_db = temp / "base.sqlite3"
        _run_side(
            _PREPARE_SCRIPT,
            {
                "side": "prepare",
                "project_root": str(project),
                "settings_module": settings_module,
                "db_env": db_env,
                "database": str(base_db),
                "seed": str(Path(seed).resolve()) if seed is not None else None,
            },
            python=source_python,
            cwd=project,
            env=base_environment,
        )
        for index, scenario in enumerate(scenarios):
            reports.append(
                _replay_one(
                    scenario,
                    index=index,
                    project=project,
                    candidate=candidate,
                    entrypoint=entrypoint,
                    settings_module=settings_module,
                    db_env=db_env,
                    base_db=base_db,
                    temp=temp,
                    ignored=ignored,
                    all_headers=all_headers,
                    source_python=source_python,
                    target_python=target_python,
                    environment=base_environment,
                )
            )
    finally:
        if not keep_temp:
            shutil.rmtree(temp, ignore_errors=True)
    matched = [report for report in reports if report["match"]]
    summary = {
        "scenarios": len(reports),
        "matched": len(matched),
        "mismatched": len(reports) - len(matched),
        "status_mismatches": sum(1 for report in reports if not report["status_match"]),
        "body_mismatches": sum(1 for report in reports if not report["body_match"]),
        "header_mismatches": sum(1 for report in reports if not report["headers_match"]),
        "database_mismatches": sum(1 for report in reports if not report["database_match"]),
        "non_native": sum(1 for report in reports if not report["native"]["compliant"]),
        "generated_probes": sum(1 for report in reports if report.get("generated_from")),
    }
    return {
        "schema": REPLAY_SCHEMA,
        "ok": summary["mismatched"] == 0 and summary["non_native"] == 0,
        "project_root": str(project),
        "candidate_root": str(candidate),
        "entrypoint": entrypoint,
        "settings_module": settings_module,
        "database": {
            "isolation_env": db_env,
            "ignored_tables": list(ignored),
            "seed": str(seed) if seed else None,
        },
        "headers": "all" if all_headers else "declared",
        "summary": summary,
        "scenarios": reports,
        "summary_lines": _summary_lines(summary, reports),
    }


def _replay_one(
    scenario: Mapping[str, Any],
    *,
    index: int,
    project: Path,
    candidate: Path,
    entrypoint: str,
    settings_module: str,
    db_env: str,
    base_db: Path,
    temp: Path,
    ignored: tuple[str, ...],
    all_headers: bool,
    source_python: Path,
    target_python: Path,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    identifier = str(scenario.get("id") or f"scenario-{index}")
    source_db = temp / f"source-{index}.sqlite3"
    candidate_db = temp / f"candidate-{index}.sqlite3"
    shutil.copy2(base_db, source_db)
    shutil.copy2(base_db, candidate_db)
    request = {
        "method": scenario["method"],
        "path": scenario["path"],
        "headers": dict(scenario.get("headers") or {}),
        "body": scenario.get("body"),
        "multipart": scenario.get("multipart"),
    }
    setup = [
        {
            "method": step["method"],
            "path": step["path"],
            "headers": dict(step.get("headers") or {}),
            "body": step.get("body"),
            "multipart": step.get("multipart"),
        }
        for step in scenario.get("setup") or []
    ]
    common = {"request": request, "setup": setup, "db_env": db_env, "boundary": _MULTIPART_BOUNDARY}
    source_result = _run_side(
        _SOURCE_SCRIPT,
        {
            **common,
            "side": f"source[{identifier}]",
            "project_root": str(project),
            "settings_module": settings_module,
            "database": str(source_db),
        },
        python=source_python,
        cwd=project,
        env=environment,
    )
    candidate_result = _run_side(
        _CANDIDATE_SCRIPT,
        {
            **common,
            "side": f"candidate[{identifier}]",
            "project_root": str(project),
            "candidate_root": str(candidate),
            "entrypoint": entrypoint,
            "database": str(candidate_db),
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
        compared = list(scenario.get("capture_headers") or [])
    header_diffs = {
        name: {"source": source_headers.get(name, ""), "candidate": candidate_headers.get(name, "")}
        for name in compared
        if source_headers.get(name, "") != candidate_headers.get(name, "")
    }
    source_snapshot = snapshot_database(source_db, ignored)
    candidate_snapshot = snapshot_database(candidate_db, ignored)
    database_diffs = diff_snapshots(source_snapshot, candidate_snapshot)
    native = dict(candidate_result.get("native") or {})
    native_compliant = bool(native.get("is_apiroute")) and bool(native.get("endpoint_in_candidate"))
    status_match = int(source_result["status"]) == int(candidate_result["status"])
    body_match = source_body == candidate_body
    headers_match = not header_diffs
    database_match = not database_diffs
    report: dict[str, Any] = {
        "id": identifier,
        "method": scenario["method"],
        "path": scenario["path"],
        "match": status_match and body_match and headers_match and database_match,
        "status_match": status_match,
        "body_match": body_match,
        "headers_match": headers_match,
        "database_match": database_match,
        "source": {
            "status": source_result["status"],
            "headers": {name: source_headers.get(name, "") for name in compared},
        },
        "candidate": {
            "status": candidate_result["status"],
            "headers": {name: candidate_headers.get(name, "") for name in compared},
        },
        "body_difference": None if body_match else body_difference(source_body, candidate_body),
        "header_differences": header_diffs,
        "database_differences": database_diffs,
        "native": {**native, "compliant": native_compliant},
    }
    if scenario.get("generated_from"):
        report["generated_from"] = scenario["generated_from"]
    return report


def _summary_lines(summary: Mapping[str, Any], reports: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = [
        f"{summary['matched']}/{summary['scenarios']} scenarios match "
        f"(status {summary['status_mismatches']}, body {summary['body_mismatches']}, "
        f"headers {summary['header_mismatches']}, "
        f"database {summary['database_mismatches']} mismatches; "
        f"{summary['non_native']} served outside a FastAPI APIRoute in the candidate)"
    ]
    for report in reports:
        if report["match"] and report["native"]["compliant"]:
            continue
        problems: list[str] = []
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
        if not report["native"]["compliant"]:
            problems.append(
                f"served by {report['native'].get('route_class')} "
                f"(endpoint in candidate: {report['native'].get('endpoint_in_candidate')})"
            )
        lines.append(
            f"{report['id']} [{report['method']} {report['path']}]: " + "; ".join(problems)
        )
    return lines

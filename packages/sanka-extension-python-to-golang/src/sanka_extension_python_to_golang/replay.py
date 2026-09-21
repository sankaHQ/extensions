# SPDX-License-Identifier: Apache-2.0
"""Bounded replay of the qualified GET contract using real framework clients."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from textwrap import indent
from typing import Any
from urllib.parse import urlencode, urlsplit

from .capture import canonical, capture, digest
from .render import render
from .security import compare_headers, header_probe, security_cases, security_environment
from .toolchain import ensure_go

SOURCE_PROBE = """
import importlib.util, json, os, sys
from urllib.parse import urlsplit, parse_qsl, unquote
from pathlib import Path
framework, filename, routes, destination, models_file, use_database = sys.argv[1:]
source_root = Path(filename).parent
while (source_root / "__init__.py").is_file():
    source_root = source_root.parent
sys.path.insert(0, str(source_root))
def module_name(path):
    path = Path(path)
    return (".".join(path.relative_to(source_root).with_suffix("").parts)
            if path.is_relative_to(source_root) else path.stem)
if framework == "drf":
    from django.conf import settings
    databases = {}
    if use_database == "1":
        url = urlsplit(os.environ["DATABASE_URL"])
        databases = {"default": {"ENGINE": "django.db.backends.postgresql",
            "NAME": unquote(url.path.lstrip("/")), "USER": unquote(url.username or ""),
            "PASSWORD": unquote(url.password or ""), "HOST": url.hostname, "PORT": url.port,
            "OPTIONS": dict(parse_qsl(url.query))}}
    settings.configure(SECRET_KEY="replay-only", ROOT_URLCONF="migration_source",
        ALLOWED_HOSTS=["testserver"], INSTALLED_APPS=[], DATABASES=databases,
        REST_FRAMEWORK={"UNAUTHENTICATED_USER": None})
    import django
    django.setup()
if models_file:
    model_spec = importlib.util.spec_from_file_location(module_name(models_file), models_file)
    model_module = importlib.util.module_from_spec(model_spec)
    sys.modules[model_spec.name] = model_module
    model_spec.loader.exec_module(model_module)
spec = importlib.util.spec_from_file_location(module_name(filename), filename)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
sys.modules["migration_source"] = module
spec.loader.exec_module(module)
if framework == "drf":
    from django.test import Client
    client = Client()
elif framework == "fastapi":
    from fastapi.testclient import TestClient
    client = TestClient(module.app)
else:
    client = module.app.test_client()
observed_headers = []
header_names = json.loads(os.environ.get("SANKA_GO_REPLAY_HEADERS", "[]"))
observed = []
for case in json.loads(routes):
    path = case if isinstance(case, str) else case["path"]
    response = client.get(path,
        headers={} if isinstance(case, str) else case.get("headers", {}), follow_redirects=False)
    observed_headers.append({key: response.headers.get(key, "") for key in header_names})
    body = response.data if framework == "flask" else response.content
    observed.append({"path": path, "status": response.status_code,
        "media_type": response.headers.get("Content-Type", "").split(";")[0],
        "body": json.loads(body)})
Path(destination).write_text(json.dumps(observed, allow_nan=False))
if header_names:
    Path(destination).with_suffix(".headers.json").write_text(json.dumps(
        {"schema": "sanka.go-security-headers/v1", "responses": observed_headers}))
if use_database == "1":
    if framework == "drf":
        from django.db import connections
        connections.close_all()
    else:
        disposed_engines = set()
        for loaded in tuple(sys.modules.values()):
            origin = getattr(loaded, "__file__", None)
            if origin and Path(origin).is_relative_to(source_root):
                engine = getattr(loaded, "engine", None)
                if engine is not None and id(engine) not in disposed_engines:
                    disposed_engines.add(id(engine))
                    import asyncio, inspect
                    disposed = engine.dispose()
                    if inspect.isawaitable(disposed):
                        asyncio.run(disposed)
"""


def _client_lifecycle(probe: str) -> str:
    """Run either probe's requests inside the framework lifespan context."""
    setup, requests = probe.split("observed = []", 1)
    requests, cleanup = ("observed = []" + requests).split("Path(destination).write_text", 1)
    return (
        setup
        + "from contextlib import nullcontext\n"
        + "with client if framework == 'fastapi' else nullcontext():\n"
        + indent(requests, "    ")
        + "Path(destination).write_text"
        + cleanup
    )


def _run(
    command: list[str], cwd: Path, *, timeout: int = 180, environment: dict[str, str] | None = None
) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=os.environ
            | {
                "GOTOOLCHAIN": "local",
                "GOWORK": "off",
                "GOMAXPROCS": "2",
                "GOFLAGS": "-mod=readonly",
                "GOENV": "off",
            }
            | (environment or {}),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"replay process failed: {error}") from error
    if result.returncode:
        details = (result.stdout + result.stderr)[-4000:]
        if environment and (
            "DATABASE_URL" in environment
            or "AUTH_READ_TOKEN" in environment
            or "AUTH_JWT_SECRET" in environment
        ):
            # Database exceptions may quote credentials or connection parameters.
            details = "database replay failed (subprocess output withheld)"
        raise ValueError("replay process failed: " + details)

    return result.stdout


def _write_source_files(root: Path, files: dict[str, bytes]) -> None:
    for name, content in files.items():
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def _source_python() -> str:
    """Select an explicit source environment without resolving venv interpreter symlinks."""
    configured = os.environ.get("SANKA_GO_SOURCE_PYTHON")
    if configured is None:
        return sys.executable
    executable = Path(configured)
    if (
        not executable.is_absolute()
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
    ):
        raise ValueError("SANKA_GO_SOURCE_PYTHON must name an absolute executable Python path")
    # Resolving a .venv/bin/python symlink would lose that environment's site-packages.
    return str(executable)


def _probe(
    target: str,
    paths: list[str],
    database: bool = False,
    request_headers: list[dict[str, str]] | None = None,
) -> str:
    request = (
        """response, err := app.Test(request)
        if err != nil { t.Fatal(err) }
        status, mediaType := response.StatusCode, response.Header.Get("Content-Type")
        body, err := io.ReadAll(io.LimitReader(response.Body, 1048577))
        response.Body.Close()
        if err != nil { t.Fatal(err) }"""
        if target == "fiber"
        else """response := httptest.NewRecorder()
        app.ServeHTTP(response, request)
        status, mediaType := response.Code, response.Header().Get("Content-Type")
        body := response.Body.Bytes()"""
    )
    io_import = '"io"' if target == "fiber" else ""
    setup = "app := NewApp()"
    database_import = ""
    if database:
        database_import = '"context"; "github.com/jackc/pgx/v5/pgxpool"; "time";'
        setup = """ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
    defer cancel()
    pool, err := pgxpool.New(ctx, os.Getenv("DATABASE_URL"))
    if err != nil { t.Fatal("invalid test database configuration") }
    defer pool.Close()
    if err := pool.Ping(ctx); err != nil { t.Fatal("test database unavailable") }
    app := NewApp(pool)"""
    serialized_headers = canonical(request_headers or [{} for _ in paths])
    return f"""package backend
import ("testing"; "net/http/httptest"; "encoding/json"; "os"; "strings";
{database_import} {io_import})
func TestSankaContractReplay(t *testing.T) {{
    {setup}
    paths := []string{{{",".join(canonical(path) for path in paths)}}}
    observed := []map[string]any{{}}
    var requestHeaders []map[string]string
    if err := json.Unmarshal([]byte({canonical(serialized_headers)}), &requestHeaders);
        err != nil {{ t.Fatal(err) }}
    for index, path := range paths {{
        request := httptest.NewRequest("GET", path, nil)
        for key, value := range requestHeaders[index] {{ request.Header.Set(key,value) }}
        {request}
        if len(body) > 1048576 || !json.Valid(body) {{
            t.Fatal("invalid or oversized JSON response")
        }}
        observed = append(observed, map[string]any{{"path": path, "status": status,
            "media_type": strings.Split(mediaType, ";")[0], "body": json.RawMessage(body)}})
    }}
    content, err := json.Marshal(observed)
    if err != nil {{ t.Fatal(err) }}
    if err := os.WriteFile("sanka-observed.json", content, 0600); err != nil {{ t.Fatal(err) }}
}}
"""


def _snapshot(output: Path) -> dict[str, bytes]:
    if output.is_symlink() or not output.is_dir():
        raise ValueError("candidate must be a regular directory")
    snapshot: dict[str, bytes] = {}
    size = 0
    for directory, names, filenames in os.walk(output, followlinks=False):
        if any((Path(directory) / name).is_symlink() for name in names):
            raise ValueError("candidate symlinks are unsupported")
        for name in filenames:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("candidate must contain only regular files")
            size += path.stat().st_size
            if size > 10_000_000 or len(snapshot) >= 1000:
                raise ValueError("candidate exceeds replay limits")
            snapshot[path.relative_to(output).as_posix()] = path.read_bytes()
    return snapshot


def request_paths(route: dict[str, Any]) -> list[str]:
    path = route["path"]
    lookup = route.get("read", {}).get("lookup")
    if lookup:
        return [path.replace(f":{lookup}", "1")]
    filtered = route.get("read", {}).get("filter")
    result = [path]
    if "pagination" in route.get("read", {}):
        result.extend(
            path + "?" + query
            for query in (
                "limit=1&offset=0",
                "limit=1&offset=1",
                "limit=2&offset=2",
                "limit=0001&offset=0000000001",
                "limit=1000&offset=2147483647",
                "limit=bad&limit=1",
            )
        )
    if not filtered:
        return result
    key = filtered["parameter"]
    for value in (
        "",
        "first",
        "last",
        "sanka-filter",
        "日本語",
        "😀",
        "x' OR '1'='1",
        "a;b",
        "a b",
        "a+b",
        "SANKA-FILTER",
        "%ZZ",
        "�",
    ):
        result.append(path + "?" + urlencode([(key, value)]))
    result.append(path + "?" + urlencode([(key, "first"), (key, "last")]))
    result.append(path + "?" + urlencode([(key, "last"), (key, "first")]))
    encoded_key = urlencode([(key, "")])
    # Native parsers accept semicolons and malformed percent escapes as values.
    for raw in ("a;b", "%ZZ", "%FF", "%E2%82", "%ED%A0%80"):
        result.append(path + "?" + encoded_key + raw)
    return result


def replay(root: Path, output: Path, captured: dict[str, Any], command: str) -> dict[str, Any]:
    if captured["gaps"]:
        raise ValueError("cannot replay unsupported source behavior")
    if any("write" in route for route in captured["routes"]):
        from .write_replay import replay_writes

        return replay_writes(root, output, captured, command)
    if not output.is_dir():
        raise ValueError("apply the reviewed plan before testing")
    source_python = _source_python() if command == "verify" else None
    snapshot = _snapshot(output)
    # Bind evidence to the exact bytes tested, including manually repaired handlers.
    candidate_hash = digest(
        {key: hashlib.sha256(value).hexdigest() for key, value in snapshot.items()}
    )
    config = captured["configuration"]
    database = any("read" in route for route in captured["routes"])
    source_environment: dict[str, str] = {}
    target_environment: dict[str, str] = {}
    if database:
        target_url = os.environ.get("SANKA_GO_TARGET_TEST_DATABASE_URL", "")
        source_url = os.environ.get("SANKA_GO_SOURCE_TEST_DATABASE_URL", "")
        if not target_url or (command == "verify" and not source_url):
            raise ValueError(
                "database replay requires explicit SANKA_GO_TARGET_TEST_DATABASE_URL "
                "and, for verify, SANKA_GO_SOURCE_TEST_DATABASE_URL fixture databases"
            )
        urls = [(target_url, {"postgres", "postgresql"})]
        if command == "verify":
            urls.append(
                (
                    source_url,
                    {"postgres", "postgresql"}
                    if config["source_framework"] == "drf"
                    else {"postgresql+psycopg"},
                )
            )
        for url, schemes in urls:
            try:
                parsed = urlsplit(url)
                valid = (
                    parsed.scheme in schemes
                    and parsed.hostname
                    and parsed.path.strip("/")
                    and not parsed.fragment
                )
                _ = parsed.port
            except ValueError:
                valid = False
            if not valid:
                raise ValueError(
                    "fixture database URL must explicitly identify a PostgreSQL host "
                    "and database, using the qualified driver"
                )
        target_environment = {"DATABASE_URL": target_url}
        source_environment = {"DATABASE_URL": source_url}
    source_environment.update(security_environment(captured))
    target_environment.update(security_environment(captured))
    expected_files = render(captured)
    for name in ("go.mod", "go.sum", "contract.json"):
        if snapshot.get(name) != expected_files[name].encode():
            raise ValueError(f"candidate {name} differs from the applied plan")
    source_bytes = (root / config["source_file"]).read_bytes()
    module_bytes = {name: (root / name).read_bytes() for name in captured.get("source_modules", [])}
    model_bytes = (
        (root / config["models_file"]).read_bytes() if config["database_layer"] == "pgx" else None
    )
    if capture(root, config) != captured:
        raise ValueError("source changed before replay")
    cases = [
        (path, route["status"]) for route in captured["routes"] for path in request_paths(route)
    ]
    cases.extend(
        (route["path"] + "?" + query, 400)
        for route in captured["routes"]
        if "pagination" in route.get("read", {})
        for query in (
            "limit=0",
            "limit=1001",
            "limit=",
            "limit=1&limit=bad",
            "limit=%D9%A1",
            "offset=-1",
            "offset=2147483648",
        )
    )
    requests: list[dict[str, Any]] = [{"path": path} for path, _ in cases]
    if captured.get("security"):
        requests = security_cases(
            [{"path": path, "method": "GET", "expected_status": status} for path, status in cases],
            captured["security"]["kind"],
        )
        cases = [(case["path"], case["expected_status"]) for case in requests]
    paths = [path for path, _ in cases]
    with tempfile.TemporaryDirectory(prefix="sanka-go-replay-") as temporary:
        workspace = Path(temporary)
        candidate = workspace / "candidate"
        candidate.mkdir()
        for name, content in snapshot.items():
            destination = candidate / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        if any(
            (candidate / name).exists()
            for name in (
                "sanka_contract_probe_test.go",
                "sanka-observed.json",
                "sanka-observed.headers.json",
            )
        ):
            raise ValueError("candidate uses reserved replay filenames")
        (candidate / "sanka_contract_probe_test.go").write_text(
            header_probe(
                _probe(
                    config["target_framework"],
                    paths,
                    database,
                    [case.get("headers", {}) for case in requests],
                ),
                captured,
            )
        )
        executable, go_environment = ensure_go(root)
        target_environment.update(go_environment)
        version = _run([executable, "version"], candidate, environment=go_environment).split()
        _run(
            [executable, "test", "-count=1", "-p=2", "-timeout=60s", "./..."],
            candidate,
            environment=target_environment,
        )
        actual = json.loads((candidate / "sanka-observed.json").read_text())
        result = {
            "schema": "sanka.python-to-golang.replay/v1",
            "command": command,
            "go_version": " ".join(version),
            "source_digest": captured["source_digest"],
            "candidate_digest": candidate_hash,
            "scope": (
                "GET status, JSON body and media type for captured routes"
                if command == "verify"
                else "Go handler execution and JSON response parsing"
            ),
            "complete_backend": False,
            "candidate": actual,
            "ok": len(actual) == len(paths)
            and all(
                item["path"] == path
                and item["status"] == status
                and item["media_type"] == "application/json"
                for item, (path, status) in zip(actual, cases, strict=True)
            ),
        }
        source_headers = None
        if command == "verify":
            # Execute the exact captured source snapshot, not an import through PYTHONPATH.
            source_directory = workspace / "source"
            source_directory.mkdir()
            _write_source_files(source_directory, module_bytes)
            model_file = ""
            if model_bytes is not None:
                model_path = source_directory / config["models_file"]
                _write_source_files(source_directory, {config["models_file"]: model_bytes})
                model_file = str(model_path)
            source = source_directory / config["source_file"]
            _write_source_files(source_directory, {config["source_file"]: source_bytes})
            observed = workspace / "source-observed.json"
            _run(
                [
                    str(source_python),
                    "-I",
                    "-c",
                    _client_lifecycle(SOURCE_PROBE),
                    config["source_framework"],
                    str(source),
                    canonical(requests),
                    str(observed),
                    model_file,
                    "1" if database else "0",
                ],
                workspace,
                timeout=30,
                environment=source_environment,
            )
            result["source_python"] = {
                "executable": source_python,
                "version": _run([str(source_python), "-I", "--version"], workspace).strip(),
            }
            if captured.get("security"):
                source_headers = json.loads(observed.with_suffix(".headers.json").read_text())
            expected = json.loads(observed.read_text())
            result.update(
                source=expected, ok=result["ok"] and canonical(actual) == canonical(expected)
            )
        if captured.get("security"):
            result["security_headers"] = compare_headers(
                json.loads((candidate / "sanka-observed.headers.json").read_text()),
                source_headers,
                captured,
                len(cases),
            )
            result["ok"] = result["ok"] and result["security_headers"]["ok"]
        if database:
            result["database_scope"] = (
                "read-only GET responses against explicitly supplied fixtures; "
                "no schema or data writes"
            )
        if capture(root, config) != captured:
            raise ValueError("source changed during replay; discard observations")
        if _snapshot(output) != snapshot:
            raise ValueError("candidate changed during replay; discard observations")
        return result

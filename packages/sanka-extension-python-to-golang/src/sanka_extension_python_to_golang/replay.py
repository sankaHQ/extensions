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
from typing import Any
from urllib.parse import urlencode, urlsplit

from .capture import canonical, capture, digest
from .render import render
from .toolchain import ensure_go

SOURCE_PROBE = """
import importlib.util, json, os, sys
from urllib.parse import urlsplit, parse_qsl, unquote
from pathlib import Path
framework, filename, routes, destination, models_file, use_database = sys.argv[1:]
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
    model_spec = importlib.util.spec_from_file_location(Path(models_file).stem, models_file)
    model_module = importlib.util.module_from_spec(model_spec)
    sys.modules[model_spec.name] = model_module
    model_spec.loader.exec_module(model_module)
sys.path.insert(0, str(Path(filename).parent))
spec = importlib.util.spec_from_file_location("migration_source", filename)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
if framework == "drf":
    from django.test import Client
    client = Client()
elif framework == "fastapi":
    from fastapi.testclient import TestClient
    client = TestClient(module.app)
else:
    client = module.app.test_client()
observed = []
for path in json.loads(routes):
    response = client.get(path, follow_redirects=False)
    body = response.data if framework == "flask" else response.content
    observed.append({"path": path, "status": response.status_code,
        "media_type": response.headers.get("Content-Type", "").split(";")[0],
        "body": json.loads(body)})
Path(destination).write_text(json.dumps(observed, allow_nan=False))
if use_database == "1":
    if framework == "drf":
        from django.db import connections
        connections.close_all()
    else:
        for loaded in tuple(sys.modules.values()):
            origin = getattr(loaded, "__file__", None)
            if origin and Path(origin).parent == Path(filename).parent:
                engine = getattr(loaded, "engine", None)
                if engine is not None:
                    engine.dispose()
"""


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
        if environment and "DATABASE_URL" in environment:
            # Database exceptions may quote credentials or connection parameters.
            details = "database replay failed (subprocess output withheld)"
        raise ValueError("replay process failed: " + details)

    return result.stdout


def _probe(target: str, paths: list[str], database: bool = False) -> str:
    request = (
        """response, err := app.Test(request)
        if err != nil { t.Fatal(err) }
        status, mediaType := response.StatusCode, response.Header.Get("Content-Type")
        body, err := io.ReadAll(io.LimitReader(response.Body, 1048577))
        response.Body.Close()
        if err != nil { t.Fatal(err) }"""
        if target == "fiber"
        else """recorder := httptest.NewRecorder()
        app.ServeHTTP(recorder, request)
        status, mediaType := recorder.Code, recorder.Header().Get("Content-Type")
        body := recorder.Body.Bytes()"""
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
    return f"""package backend
import ("testing"; "net/http/httptest"; "encoding/json"; "os"; "strings";
{database_import} {io_import})
func TestSankaContractReplay(t *testing.T) {{
    {setup}
    paths := []string{{{",".join(canonical(path) for path in paths)}}}
    observed := []map[string]any{{}}
    for _, path := range paths {{
        request := httptest.NewRequest("GET", path, nil)
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
    if not filtered:
        return [path]
    key = filtered["parameter"]
    result = [path]
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
        raise ValueError(
            "write replay requires the versioned shared HTTP scenario adapter; "
            "use the qualified PostgreSQL lifecycle test until it is adopted"
        )
    if not output.is_dir():
        raise ValueError("apply the reviewed plan before testing")
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
            )
        ):
            raise ValueError("candidate uses reserved replay filenames")
        (candidate / "sanka_contract_probe_test.go").write_text(
            _probe(config["target_framework"], paths, database)
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
        if command == "verify":
            # Execute the exact captured source snapshot, not an import through PYTHONPATH.
            source_directory = workspace / "source"
            source_directory.mkdir()
            for name, content in module_bytes.items():
                (source_directory / name).write_bytes(content)
            model_file = ""
            if model_bytes is not None:
                model_path = source_directory / config["models_file"]
                model_path.write_bytes(model_bytes)
                model_file = str(model_path)
            source = source_directory / config["source_file"]
            source.write_bytes(source_bytes)
            observed = workspace / "source-observed.json"
            _run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    SOURCE_PROBE,
                    config["source_framework"],
                    str(source),
                    canonical(paths),
                    str(observed),
                    model_file,
                    "1" if database else "0",
                ],
                workspace,
                timeout=30,
                environment=source_environment,
            )
            expected = json.loads(observed.read_text())
            result.update(
                source=expected, ok=result["ok"] and canonical(actual) == canonical(expected)
            )
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

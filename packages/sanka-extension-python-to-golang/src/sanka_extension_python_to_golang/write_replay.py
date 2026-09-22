# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""Ordered write replay on explicitly supplied, resettable PostgreSQL fixtures."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sanka_http_replay import (
    cases_document,
    compare,
    default_scenarios,
    load_scenarios,
    validate_observations,
    validate_scenarios,
)

from .capture import canonical, capture, digest
from .render import render
from .replay import (
    SOURCE_PROBE,
    _client_lifecycle,
    _run,
    _snapshot,
    _source_python,
    _write_source_files,
)
from .security import compare_headers, header_probe, security_cases, security_environment
from .toolchain import ensure_go


def scenarios_for(root: Path, captured: dict[str, Any]) -> list[dict[str, Any]]:
    path = root / "sanka-verify.json"
    if path.exists():
        return load_scenarios(path)
    if any(route.get("write", {}).get("transaction") for route in captured["routes"]):
        raise ValueError("transactions require explicit ordered sanka-verify.json scenarios")
    if any("references" in field for model in captured["models"] for field in model["fields"]):
        raise ValueError("relational writes require explicit ordered sanka-verify.json scenarios")
    if any(
        route.get("write", {}).get("constraints")
        or route.get("write", {}).get("validation")
        or route.get("write", {}).get("scope")
        for route in captured["routes"]
    ):
        raise ValueError("captured write validation requires explicit sanka-verify.json scenarios")
    operations = []
    for route in captured["routes"]:
        operation = {"method": route["method"], "path": route["path"], "status": route["status"]}
        if "write" in route:
            write = route["write"]
            operation.update(
                kind={"patch": "update"}.get(write["operation"], write["operation"]),
                model=write["model"],
                conflict=write.get("integrity_conflict", False),
            )
        elif "read" in route:
            operation.update(
                kind="lookup" if route["read"].get("lookup") else "list",
                model=route["read"]["model"],
            )
        else:
            operation["kind"] = "literal"
        operations.append(operation)
    models = [
        dict(
            model,
            fields=[
                dict(
                    field,
                    type={
                        "int32": "integer",
                        "int64": "bigint",
                        "bool": "boolean",
                        "string": "string",
                    }[field["go_type"]],
                )
                for field in model["fields"]
            ],
        )
        for model in captured["models"]
    ]
    return default_scenarios(operations, models)


# Keep the existing source loader, including explicit source Python environments.
# Only trusted fixture execution imports framework and database dependencies.
SOURCE_WRITES = (
    SOURCE_PROBE[: SOURCE_PROBE.index("observed = []")]
    .replace(
        "framework, filename, routes, destination, models_file, use_database = sys.argv[1:]",
        "framework, filename, routes, destination, models_file, use_database, contract = sys.argv[1:]\nmodels = json.loads(contract)",
    )
    .replace(
        "model_spec.loader.exec_module(model_module)",
        """model_spec.loader.exec_module(model_module)
    if framework == 'drf':
        from django.db import connection
        existing = set(connection.introspection.table_names())
        with connection.schema_editor() as editor:
            for model in reversed(models):
                if model['table'] in existing:
                    editor.delete_model(getattr(model_module, model['name']))
            for model in models:
                editor.create_model(getattr(model_module, model['name']))
    else:
        from sqlalchemy import create_engine
        engine = create_engine(os.environ['DATABASE_URL'])
        tables = [getattr(model_module, model['name']).__table__ for model in models]
        model_module.Base.metadata.drop_all(engine, tables=tables)
        model_module.Base.metadata.create_all(engine, tables=tables)
        engine.dispose()""",
    )
    + """
import psycopg
from psycopg import sql
observed = []
observed_bytes = 0
with psycopg.connect(os.environ['DATABASE_URL'].replace('postgresql+psycopg://', 'postgresql://'), autocommit=True) as connection:
    for case in json.loads(Path(routes).read_text())['scenarios']:
        method, path = case['method'], case['path']
        headers = case.get('headers', {})
        body = json.dumps(case['body'], allow_nan=False) if 'body' in case else None
        if body is not None:
            headers = dict(headers, **{'Content-Type': 'application/json'})
        if framework == 'drf':
            response = client.generic(method, path, data=body or '', content_type='application/json', headers=headers)
        elif framework == 'flask':
            response = client.open(path, method=method, data=body, headers=headers, follow_redirects=False)
        else:
            response = client.request(method, path, content=body, headers=headers, follow_redirects=False)
        observed_headers.append({key: response.headers.get(key, "") for key in header_names})
        raw = response.data if framework == 'flask' else response.content
        if response.status_code in (204, 205, 304) and raw:
            raise ValueError("bodyless response carries bytes")
        if len(raw) > 1048576:
            raise ValueError('oversized response')
        tables, sequences = {}, {}
        for model in models:
            fields = model['fields']
            primary = next(field for field in fields if field['primary_key'])
            columns = sql.SQL(',').join(sql.Identifier(field['name']) for field in fields)
            rows = connection.execute(sql.SQL('SELECT {} FROM {} ORDER BY {}').format(columns, sql.Identifier(model['table']), sql.Identifier(primary['name']))).fetchall()
            tables[model['table']] = [{field['name']: str(value) if field['go_type'] == 'int64' and value is not None else value for field, value in zip(fields, row)} for row in rows]
            if primary['auto']:
                sequence = connection.execute('SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid=pg_get_serial_sequence(%s, %s)::regclass', ('"' + model['table'] + '"', primary['name'])).fetchone()
                value, called = connection.execute(sql.SQL('SELECT last_value::text,is_called FROM {}').format(sql.Identifier(*sequence))).fetchone()
                sequences[model['table']] = [value, called]
        record = {'id': case['id'], 'method': method, 'path': path, 'status': response.status_code,
            'media_type': response.headers.get('Content-Type', '').split(';')[0],
            'body': json.loads(raw) if raw else None, 'tables': tables, 'sequences': sequences}
        observed_bytes += len(json.dumps(record, allow_nan=False).encode())
        if observed_bytes > 16 * 1024 * 1024:
            raise ValueError('write observations exceed 16 MiB')
        observed.append(record)
"""
    + SOURCE_PROBE[SOURCE_PROBE.index("Path(destination).write_text") :]
)


# Reuse exactly the per-request snapshot for the initial denial baseline.
_snapshot_start = SOURCE_WRITES.index("        tables, sequences = {}, {}")
_snapshot_end = SOURCE_WRITES.index("        record =", _snapshot_start)
_snapshot_body = SOURCE_WRITES[_snapshot_start:_snapshot_end]
SOURCE_WRITES = (
    SOURCE_WRITES[:_snapshot_start]
    + "        tables, sequences = snapshot(connection)\n"
    + SOURCE_WRITES[_snapshot_end:]
)
SOURCE_WRITES = SOURCE_WRITES.replace(
    "observed = []",
    "def snapshot(connection):\n"
    + "\n".join(line[4:] for line in _snapshot_body.splitlines())
    + "\n    return tables, sequences\nobserved = []",
).replace(
    "    for case in json.loads(Path(routes).read_text())['scenarios']:",
    "    tables, sequences = snapshot(connection)\n"
    "    Path(destination).with_suffix('.initial.json').write_text(json.dumps({'tables': tables, 'sequences': sequences}))\n"
    "    for case in json.loads(Path(routes).read_text())['scenarios']:",
)


def write_probe(captured: dict[str, Any]) -> str:
    target = captured["configuration"]["target_framework"]
    exchange = """response, err := app.Test(request)
        if err != nil { t.Fatal(err) }
        status, mediaType := response.StatusCode, response.Header.Get("Content-Type")
        body, err := io.ReadAll(io.LimitReader(response.Body, 1048577)); response.Body.Close()
        if err != nil { t.Fatal(err) }"""
    if target != "fiber":
        exchange = """response := httptest.NewRecorder()
        app.ServeHTTP(response, request)
        status, mediaType, body := response.Code, response.Header().Get("Content-Type"), response.Body.Bytes()"""
    queries = []
    for model in captured["models"]:
        primary = next(f for f in model["fields"] if f["primary_key"])
        columns = ",".join(
            f'"{f["name"]}"' + (f'::text AS "{f["name"]}"' if f["go_type"] == "int64" else "")
            for f in model["fields"]
        )
        query = f'SELECT row_to_json(saved) FROM (SELECT {columns} FROM "{model["table"]}" ORDER BY "{primary["name"]}") saved'
        queries.append(f"""{{
            rows, err := pool.Query(ctx, {canonical(query)}); if err != nil {{ t.Fatal(err) }}
            saved := []json.RawMessage{{}}
            for rows.Next() {{ var row []byte; if err := rows.Scan(&row); err != nil {{ rows.Close(); t.Fatal(err) }}; saved = append(saved, json.RawMessage(row)) }}
            rows.Close(); if err := rows.Err(); err != nil {{ t.Fatal(err) }}
            tables[{canonical(model["table"])}] = saved
        }}""")
        if primary["auto"]:
            queries.append(f"""{{
                var sequence string
                if err := pool.QueryRow(ctx, "SELECT pg_get_serial_sequence($1,$2)", {canonical('"' + model["table"] + '"')}, {canonical(primary["name"])}).Scan(&sequence); err != nil {{ t.Fatal(err) }}
                var value string; var called bool
                if err := pool.QueryRow(ctx, "SELECT last_value::text,is_called FROM " + sequence).Scan(&value,&called); err != nil {{ t.Fatal(err) }}
                sequences[{canonical(model["table"])}] = []any{{value,called}}
            }}""")
    return (
        """package backend
import ("bytes"; "context"; "encoding/json"; "net/http/httptest"; "os"; "strings"; "testing"; "time"; "github.com/jackc/pgx/v5/pgxpool"; IO_IMPORT)
func TestSankaFixtureIdentity(t *testing.T) {
    ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second); defer cancel()
    pool, err := pgxpool.New(ctx, os.Getenv("DATABASE_URL")); if err != nil { t.Fatal(err) }; defer pool.Close()
    var started, database, schema string
    if err := pool.QueryRow(ctx, "SELECT extract(epoch from pg_postmaster_start_time())::text,current_database(),current_schema()").Scan(&started,&database,&schema); err != nil { t.Fatal(err) }
    data, err := json.Marshal([]any{started,database,schema}); if err != nil { t.Fatal(err) }
    if err := os.WriteFile("sanka-fixture.json", data, 0600); err != nil { t.Fatal(err) }
}
func TestSankaContractReplay(t *testing.T) {
    ctx, cancel := context.WithTimeout(context.Background(), 50*time.Second); defer cancel()
    pool, err := pgxpool.New(ctx, os.Getenv("DATABASE_URL")); if err != nil { t.Fatal(err) }; defer pool.Close()
    app := NewApp(pool)
    raw, err := os.ReadFile("sanka-cases.json"); if err != nil { t.Fatal(err) }
    var document struct { Scenarios []struct { ID, Method, Path string; Headers map[string]string; Body json.RawMessage } }
    if err := json.Unmarshal(raw, &document); err != nil { t.Fatal(err) }
    snapshot := func() (map[string]any, map[string]any) {
        tables, sequences := map[string]any{}, map[string]any{}
        QUERIES
        return tables, sequences
    }
    tables, sequences := snapshot()
    baseline, err := json.Marshal(map[string]any{"tables":tables,"sequences":sequences}); if err != nil { t.Fatal(err) }
    if err := os.WriteFile("sanka-observed.initial.json", baseline, 0600); err != nil { t.Fatal(err) }
    observed := []map[string]any{}
    observedBytes := 0
    for _, c := range document.Scenarios {
        request := httptest.NewRequest(c.Method, c.Path, bytes.NewReader(c.Body))
        for key,value := range c.Headers { request.Header.Set(key,value) }
        if len(c.Body) > 0 { request.Header.Set("Content-Type", "application/json") }
        EXCHANGE
        if (status == 204 || status == 205 || status == 304) && len(body) != 0 { t.Fatal("bodyless response carries bytes") }
        if len(body) > 1048576 { t.Fatal("oversized response") }
        if len(body) == 0 { body = []byte("null") }
        if !json.Valid(body) { t.Fatal("non-JSON response") }
        tables, sequences := snapshot()
        record := map[string]any{"id":c.ID,"method":c.Method,"path":c.Path,"status":status,
            "media_type":strings.Split(mediaType,";")[0],"body":json.RawMessage(body),"tables":tables,"sequences":sequences}
        encoded, err := json.Marshal(record); if err != nil { t.Fatal(err) }
        observedBytes += len(encoded)
        if observedBytes > 16*1024*1024 { t.Fatal("write observations exceed 16 MiB") }
        observed = append(observed, record)
    }
    data, err := json.Marshal(observed); if err != nil { t.Fatal(err) }
    if err := os.WriteFile("sanka-observed.json",data,0600); err != nil { t.Fatal(err) }
}
""".replace("IO_IMPORT", '"io"' if target == "fiber" else "")
        .replace("EXCHANGE", exchange)
        .replace("QUERIES", "\n".join(queries))
    )


def normalize_bodies(observed: list[dict[str, Any]], captured: dict[str, Any]) -> None:
    """Normalize only captured bigint model fields, never arbitrary response values."""
    models = {model["name"]: model for model in captured["models"]}
    for item in observed:
        for route in captured["routes"]:
            pattern = re.escape(route["path"])
            pattern = re.sub(r":[a-zA-Z_][a-zA-Z_0-9]*", "[^/]+", pattern)
            if route["method"] != item["method"] or not re.fullmatch(
                pattern, urlsplit(item["path"]).path
            ):
                continue
            operation = route.get("read", route.get("write", {}))
            if "literal_response" in operation:
                break
            model = models.get(operation.get("model"))
            if model is None or not 200 <= item["status"] < 300:
                break
            body = item["body"]
            rows = body if isinstance(body, list) else [body]
            for row in rows:
                if isinstance(row, dict):
                    for field in model["fields"]:
                        value = row.get(field["name"])
                        if field["go_type"] == "int64" and type(value) is int:
                            row[field["name"]] = str(value)
            break


def replay_writes(
    root: Path, output: Path, captured: dict[str, Any], command: str
) -> dict[str, Any]:
    config = captured["configuration"]
    if config["schema_mode"] != "empty":
        raise ValueError("write replay resets fixtures and requires schema_mode=empty")
    scenario_path = root / "sanka-verify.json"
    if scenario_path.exists() or scenario_path.is_symlink():
        load_scenarios(scenario_path)
    scenario_bytes = scenario_path.read_bytes() if scenario_path.exists() else None
    scenarios = (
        validate_scenarios(json.loads(scenario_bytes))
        if scenario_bytes is not None
        else scenarios_for(root, captured)
    )
    if captured.get("security"):
        scenarios = validate_scenarios(
            cases_document(
                security_cases(scenarios, captured["security"]["kind"], captured["routes"])
            )
        )
    target_url = os.environ.get("SANKA_GO_TARGET_TEST_DATABASE_URL", "")
    source_url = os.environ.get("SANKA_GO_SOURCE_TEST_DATABASE_URL", "")
    urls = [target_url] + ([source_url] if command == "verify" else [])
    for index, url in enumerate(urls):
        parsed = urlsplit(url)
        schemes = (
            {"postgres", "postgresql"}
            if index == 0 or config["source_framework"] == "drf"
            else {"postgresql+psycopg"}
        )
        if (
            parsed.scheme not in schemes
            or not parsed.hostname
            or not parsed.path.strip("/")
            or parsed.fragment
        ):
            raise ValueError(
                "write replay requires explicit SANKA_GO_TARGET_TEST_DATABASE_URL and (for verify) SANKA_GO_SOURCE_TEST_DATABASE_URL resettable PostgreSQL fixtures"
            )
        _ = parsed.port
    if command == "verify" and source_url.replace("postgresql+psycopg:", "postgresql:").replace(
        "postgres:", "postgresql:"
    ) == target_url.replace("postgres:", "postgresql:"):
        raise ValueError("source and target write fixtures must be independent")
    source_python = _source_python() if command == "verify" else None
    snapshot = _snapshot(output)
    expected = render(captured)
    for name in ("go.mod", "go.sum", "contract.json"):
        if snapshot.get(name) != expected[name].encode():
            raise ValueError(f"candidate {name} differs from the applied plan")
    if capture(root, config) != captured:
        raise ValueError("source changed before replay")
    files = {
        name: (root / name).read_bytes()
        for name in [
            config["source_file"],
            config["models_file"],
            *captured.get("source_modules", []),
        ]
    }
    with tempfile.TemporaryDirectory(prefix="sanka-go-write-replay-") as temporary:
        workspace = Path(temporary)
        candidate = workspace / "candidate"
        candidate.mkdir()
        for name, content in snapshot.items():
            destination = candidate / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        for name in (
            "sanka_contract_probe_test.go",
            "sanka-cases.json",
            "sanka-observed.json",
            "sanka-observed.headers.json",
            "sanka-fixture.json",
        ):
            if name in snapshot:
                raise ValueError("candidate uses reserved replay filenames")
        (candidate / "sanka_contract_probe_test.go").write_text(
            header_probe(write_probe(captured), captured)
        )
        cases = candidate / "sanka-cases.json"
        cases.write_text(canonical(cases_document(scenarios)))
        executable, environment = ensure_go(root)
        target_env = environment | {"DATABASE_URL": target_url} | security_environment(captured)
        version = _run([executable, "version"], candidate, environment=environment).strip()
        # Compare actual connection identities before either fixture is reset.
        if command == "verify":
            identities = []
            for url in (target_url, source_url.replace("postgresql+psycopg:", "postgresql:")):
                _run(
                    [
                        executable,
                        "test",
                        "-count=1",
                        "-p=2",
                        "-run",
                        "^TestSankaFixtureIdentity$",
                        ".",
                    ],
                    candidate,
                    environment=environment | {"DATABASE_URL": url},
                )
                identities.append(json.loads((candidate / "sanka-fixture.json").read_text()))
            if identities[0] == identities[1]:
                raise ValueError(
                    "source and target write fixtures resolve to the same database schema"
                )
        # Ensure the baseline exists before rolling it down, including a fresh fixture.
        for direction in ("up", "down", "up"):
            _run(
                [executable, "run", "-p=2", "./cmd/migrate", direction],
                candidate,
                environment=target_env,
            )
        _run(
            [executable, "test", "-count=1", "-p=2", "-timeout=60s", "./..."],
            candidate,
            environment=target_env,
        )
        actual = validate_observations(
            json.loads((candidate / "sanka-observed.json").read_text()), scenarios
        )
        normalize_bodies(actual, captured)
        result: dict[str, Any] = {
            "schema": "sanka.python-to-golang.replay/v1",
            "command": command,
            "go_version": version,
            "source_digest": captured["source_digest"],
            "candidate_digest": digest(
                {key: hashlib.sha256(value).hexdigest() for key, value in snapshot.items()}
            ),
            "scenario_digest": digest(cases_document(scenarios)),
            "scenarios": cases_document(scenarios),
            "scope": "ordered HTTP responses, captured rows and sequences on reset fixture databases",
            "database_scope": "captured fixture tables reset before writes; final effects retained for inspection",
            "complete_backend": False,
            "candidate": actual,
        }
        initial = json.loads((candidate / "sanka-observed.initial.json").read_text())
        source_initial = {}
        source_observed = None
        source_headers = None
        if command == "verify":
            source = workspace / "source"
            source.mkdir()
            _write_source_files(source, files)
            observed = workspace / "source-observed.json"
            _run(
                [
                    str(source_python),
                    "-I",
                    "-c",
                    _client_lifecycle(SOURCE_WRITES),
                    config["source_framework"],
                    str(source / config["source_file"]),
                    str(cases),
                    str(observed),
                    str(source / config["models_file"]),
                    "1",
                    canonical(captured["models"]),
                ],
                workspace,
                timeout=60,
                environment={"DATABASE_URL": source_url} | security_environment(captured),
            )
            if captured.get("security"):
                source_headers = json.loads(observed.with_suffix(".headers.json").read_text())
            source_initial = json.loads(observed.with_suffix(".initial.json").read_text())
            source_observed = validate_observations(json.loads(observed.read_text()), scenarios)
            normalize_bodies(source_observed, captured)
            result["source"] = source_observed
            result["source_python"] = {
                "executable": source_python,
                "version": _run([str(source_python), "-I", "--version"], workspace).strip(),
            }
        result.update(compare(scenarios, actual, source_observed))
        if any(route.get("write", {}).get("integrity_conflict") for route in captured["routes"]):
            statuses = (
                (404, 409)
                if any(route.get("write", {}).get("transaction") for route in captured["routes"])
                else (409,)
            )
            unchanged = integrity_failures_unchanged(
                initial, actual, statuses=statuses
            ) and integrity_failures_unchanged(
                source_initial, source_observed or [], statuses=statuses
            )
            result["integrity_failures_unchanged"] = unchanged
            result["ok"] = result["ok"] and unchanged
        if captured.get("security"):
            result["security_headers"] = compare_headers(
                json.loads((candidate / "sanka-observed.headers.json").read_text()),
                source_headers,
                captured,
                len(scenarios),
            )
            # Include the pre-request snapshot, so even the first denial must be inert.
            unchanged = denied_writes_unchanged(initial, actual) and denied_writes_unchanged(
                source_initial, source_observed or []
            )
            result["denied_writes_unchanged"] = unchanged
            result["ok"] = result["ok"] and result["security_headers"]["ok"] and unchanged
        if (
            capture(root, config) != captured
            or _snapshot(output) != snapshot
            or (scenario_path.read_bytes() if scenario_path.exists() else None) != scenario_bytes
        ):
            raise ValueError(
                "source, candidate or scenarios changed during replay; discard observations"
            )
        return result


def denied_writes_unchanged(initial: dict[str, Any], observations: list[dict[str, Any]]) -> bool:
    return all(
        current["tables"] == previous["tables"] and current["sequences"] == previous["sequences"]
        for previous, current in pairwise([initial, *observations])
        if current["status"] in (401, 403)
        or (".cross-" in current["id"] and current["status"] == 404)
    )


def integrity_failures_unchanged(
    initial: dict[str, Any],
    observations: list[dict[str, Any]],
    *,
    statuses: tuple[int, ...] = (409,),
) -> bool:
    # PostgreSQL sequence allocation is not transactional; rows must still roll back.
    return all(
        current["tables"] == previous["tables"]
        for previous, current in pairwise([initial, *observations])
        if current["status"] in statuses
    )

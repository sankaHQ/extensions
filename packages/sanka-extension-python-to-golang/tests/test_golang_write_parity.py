# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""Real Python/Go HTTP writes and per-request PostgreSQL effects, without servers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sanka_extension_python_to_golang.capture import SOURCES, TARGETS
from sanka_extension_python_to_golang.replay import SOURCE_PROBE
from test_golang_drf_validation import drf_serializer_source
from test_golang_routing import group_backend
from test_golang_schema import SOURCE_DDL, generate, schema_dsn
from test_golang_validation import schema_source
from test_golang_writes import drf_write_source, fastapi_write_source, flask_write_source

SCENARIOS = [
    {"method": "POST", "path": "/api/widgets", "body": {"name": "missing"}, "status": 400},
    {
        "method": "POST",
        "path": "/api/widgets",
        "body": {"name": "wrong", "count": True, "enabled": False},
        "status": 400,
    },
    {
        "method": "POST",
        "path": "/api/widgets",
        "body": {"name": "alpha", "count": 7, "enabled": True, "note": None},
        "status": 201,
    },
    {
        "method": "PATCH",
        "path": "/api/widgets/1",
        "body": {"count": 0, "enabled": False, "note": ""},
        "status": 200,
    },
    {"method": "PATCH", "path": "/api/widgets/1", "body": {}, "status": 200},
    {"method": "PATCH", "path": "/api/widgets/1", "body": {"note": None}, "status": 200},
    {"method": "PATCH", "path": "/api/widgets/1", "body": {"name": None}, "status": 400},
    {"method": "PATCH", "path": "/api/widgets/1", "body": {"extra": 1}, "status": 400},
    {"method": "PATCH", "path": "/api/widgets/999", "body": {"count": 1}, "status": 404},
    {"method": "PUT", "path": "/api/widgets/1", "body": {"name": "missing"}, "status": 400},
    {
        "method": "PUT",
        "path": "/api/widgets/1",
        "body": {"name": "beta", "count": 9, "enabled": False},
        "status": 200,
    },
    {
        "method": "PUT",
        "path": "/api/widgets/999",
        "body": {"name": "missing", "count": 1, "enabled": True},
        "status": 404,
    },
    {"method": "DELETE", "path": "/api/widgets/1", "status": 204},
    {"method": "DELETE", "path": "/api/widgets/1", "status": 404},
    {
        "method": "POST",
        "path": "/api/widgets",
        "body": {"name": "next", "count": -2147483648, "enabled": False},
        "status": 201,
    },
    {
        "method": "POST",
        "path": "/api/widgets",
        "body": {"name": "overflow", "count": 2147483648, "enabled": True},
        "status": 400,
    },
]

# Reuse the existing source loader and cleanup. This is an independent qualification
# fixture, not a competing shared replay protocol or public write-verification API.
SOURCE_WRITES = (
    SOURCE_PROBE[: SOURCE_PROBE.index("observed = []")]
    + """
import psycopg
observed = []
with psycopg.connect(os.environ['DATABASE_URL'].replace('postgresql+psycopg://', 'postgresql://'), autocommit=True) as connection:
    for case in json.loads(routes):
        method, path = case['method'], case['path']
        if framework == 'drf':
            body = json.dumps(case['body']) if 'body' in case else ''
            response = client.generic(method, path, data=body, content_type='application/json')
        elif framework == 'flask':
            response = client.open(path, method=method, follow_redirects=False, **({'json':case['body']} if 'body' in case else {}))
        else:
            response = client.request(method, path, follow_redirects=False, **({'json':case['body']} if 'body' in case else {}))
        body = response.data if framework == 'flask' else response.content
        rows = connection.execute('SELECT id,name,count,enabled,note FROM widgets ORDER BY id').fetchall()
        sequence = connection.execute('SELECT last_value,is_called FROM widgets_id_seq').fetchone()
        observed.append({'method': method, 'path': path, 'status': response.status_code,
            'media_type': response.headers.get('Content-Type','').split(';')[0],
            'body': json.loads(body) if body else None,
            'rows': [dict(zip(('id','name','count','enabled','note'), row)) for row in rows],
            'sequence': list(sequence)})
"""
    + SOURCE_PROBE[SOURCE_PROBE.index("Path(destination).write_text") :]
)


def write_probe(target: str, *, tamper: bool = False) -> str:
    exchange = """response, err := app.Test(request)
        if err != nil { t.Fatal(err) }
        status, mediaType := response.StatusCode, response.Header.Get("Content-Type")
        body, err := io.ReadAll(response.Body); response.Body.Close()
        if err != nil { t.Fatal(err) }"""
    if target != "fiber":
        exchange = """response := httptest.NewRecorder()
        app.ServeHTTP(response, request)
        status, mediaType, body := response.Code, response.Header().Get("Content-Type"), response.Body.Bytes()"""
    return (
        """package backend
import ("bytes"; "context"; "encoding/json"; "net/http/httptest"; "os"; "strings"; "testing"; "time"; "github.com/jackc/pgx/v5/pgxpool"; IO_IMPORT)
func TestWriteParity(t *testing.T) {
    ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second); defer cancel()
    pool, err := pgxpool.New(ctx, os.Getenv("DATABASE_URL")); if err != nil { t.Fatal(err) }; defer pool.Close()
    app := NewApp(pool)
    raw, err := os.ReadFile("write-cases.json"); if err != nil { t.Fatal(err) }
    var cases []struct { Method, Path string; Body json.RawMessage }
    if err := json.Unmarshal(raw, &cases); err != nil { t.Fatal(err) }
    observed := []map[string]any{}
    for _, c := range cases {
        request := httptest.NewRequest(c.Method, c.Path, bytes.NewReader(c.Body))
        request.Header.Set("Content-Type", "application/json")
        EXCHANGE
        if len(body) == 0 { body = []byte("null") }
        if !json.Valid(body) { t.Fatalf("non-JSON response: %s", body) }
        TAMPER
        rows, err := pool.Query(ctx, "SELECT id,name,count,enabled,note FROM widgets ORDER BY id"); if err != nil { t.Fatal(err) }
        saved := []Widget{}
        for rows.Next() {
            var item Widget
            if err := rows.Scan(&item.Id,&item.Name,&item.Count,&item.Enabled,&item.Note); err != nil { t.Fatal(err) }
            saved = append(saved, item)
        }
        rows.Close(); if err := rows.Err(); err != nil { t.Fatal(err) }
        var value int64; var called bool
        if err := pool.QueryRow(ctx,"SELECT last_value,is_called FROM widgets_id_seq").Scan(&value,&called); err != nil { t.Fatal(err) }
        observed = append(observed, map[string]any{"method":c.Method,"path":c.Path,"status":status,
            "media_type":strings.Split(mediaType,";")[0],"body":json.RawMessage(body),"rows":saved,"sequence":[]any{value,called}})
    }
    data, err := json.Marshal(observed); if err != nil { t.Fatal(err) }
    if err := os.WriteFile("write-observed.json",data,0600); err != nil { t.Fatal(err) }
}
""".replace("IO_IMPORT", '"io"' if target == "fiber" else "")
        .replace("EXCHANGE", exchange)
        .replace(
            "TAMPER",
            """if status == 201 { if _, err := pool.Exec(ctx, "UPDATE widgets SET note='tampered'"); err != nil { t.Fatal(err) } }"""
            if tamper
            else "",
        )
    )


def qualify_writes(
    tmp_path: Path, framework: str, target: str, schemas: bool, *, tamper: bool = False
):
    import psycopg
    from psycopg import sql

    source = (
        (drf_serializer_source() if framework == "drf" else schema_source(framework))
        if schemas
        else {
            "drf": drf_write_source,
            "flask": flask_write_source,
            "fastapi": fastapi_write_source,
        }[framework]()
    )
    output = generate(tmp_path, framework, target, app_source=group_backend(source, framework))
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    names = ["go_write_parity_" + uuid.uuid4().hex for _ in range(2)]
    environment = os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"}
    with psycopg.connect(dsn, autocommit=True) as admin:
        created = []
        try:
            for name in names:
                admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
                created.append(name)
            source_url, target_url = [schema_dsn(dsn, name) for name in names]
            subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    SOURCE_DDL,
                    framework,
                    str(tmp_path / "models.py"),
                    source_url,
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            subprocess.run(
                ["go", "run", "-mod=readonly", "-p=2", "./cmd/migrate", "up"],
                cwd=output,
                env=environment | {"DATABASE_URL": target_url},
                check=True,
                capture_output=True,
                timeout=180,
            )
            source_report = tmp_path / "source-writes.json"
            source_env = environment | {
                "DATABASE_URL": source_url
                if framework == "drf"
                else source_url.replace("postgresql://", "postgresql+psycopg://", 1)
            }
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    SOURCE_WRITES,
                    framework,
                    str(tmp_path / "app.py"),
                    json.dumps(SCENARIOS),
                    str(source_report),
                    str(tmp_path / "models.py"),
                    "1",
                ],
                cwd=tmp_path,
                env=source_env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            (output / "write-cases.json").write_text(json.dumps(SCENARIOS))
            (output / "write_parity_test.go").write_text(write_probe(target, tamper=tamper))
            result = subprocess.run(
                ["go", "test", "-mod=readonly", "-count=1", "-p=2", "-run", "TestWriteParity", "."],
                cwd=output,
                env=environment | {"DATABASE_URL": target_url},
                capture_output=True,
                text=True,
                timeout=180,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            expected = json.loads(source_report.read_text())
            actual = json.loads((output / "write-observed.json").read_text())
            assert [item["status"] for item in expected] == [case["status"] for case in SCENARIOS]
            assert len(expected) == len(actual) == len(SCENARIOS)
            return expected, actual
        finally:
            for name in created:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name))
                )


requires_database = pytest.mark.skipif(
    os.getenv("SANKA_GO_TESTS") != "1" or not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN"),
    reason="requires qualified Go toolchain and explicit PostgreSQL fixture",
)


@requires_database
@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("schemas", [False, True])
def test_source_target_write_effects(
    tmp_path: Path, framework: str, target: str, schemas: bool
) -> None:
    expected, actual = qualify_writes(tmp_path, framework, target, schemas)
    assert actual == expected


@requires_database
def test_matching_response_cannot_hide_changed_database(tmp_path: Path) -> None:
    expected, actual = qualify_writes(tmp_path, "fastapi", "fiber", True, tamper=True)
    # The first successful HTTP response matches; the side effect still fails parity.
    index = next(i for i, item in enumerate(expected) if item["status"] == 201)
    assert {key: value for key, value in actual[index].items() if key != "rows"} == {
        key: value for key, value in expected[index].items() if key != "rows"
    }
    assert actual[index]["rows"] != expected[index]["rows"]


@pytest.mark.skipif(os.getenv("SANKA_GO_TESTS") != "1", reason="requires qualified Go toolchain")
@pytest.mark.parametrize("target", TARGETS)
def test_write_probe_compiles_without_database(tmp_path: Path, target: str) -> None:
    output = generate(tmp_path, "fastapi", target, app_source=fastapi_write_source())
    (output / "write_parity_test.go").write_text(write_probe(target))
    result = subprocess.run(
        ["go", "test", "-mod=readonly", "-p=2", "-run", "^$", "./..."],
        cwd=output,
        env=os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr

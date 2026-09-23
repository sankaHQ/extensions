# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""Isolated SQLite source versus PostgreSQL target replay for Django projects."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sanka_http_replay import (
    MAX_SCENARIOS,
    cases_document,
    compare,
    validate_observations,
    validate_scenarios,
)


def scenario_groups(root: Path, captured: dict[str, Any]) -> list[list[dict[str, Any]]]:
    document = root / "sanka-verify.json"
    if document.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("scenario document exceeds 16 MiB")
    payload = json.loads(document.read_text())
    if not isinstance(payload, dict):
        raise ValueError("scenario document must be an object")
    payload = dict(payload)
    env = payload.pop("db_env", None)
    if env is not None and env != captured["drf_project"]["database"].get("environment"):
        raise ValueError("scenario db_env differs from captured database configuration")
    cases = payload.get("scenarios")
    if not isinstance(cases, list) or not cases:
        raise ValueError("explicit HTTP scenarios are required")
    if not any(isinstance(case, dict) and "setup" in case for case in cases):
        return [validate_scenarios(payload)]
    if set(payload) != {"scenarios"}:
        raise ValueError("unsupported independent scenario document options")
    groups = []
    ids = set()
    for original in cases:
        if not isinstance(original, dict):
            raise ValueError("scenario must be an object")
        case = dict(original)
        setup = case.pop("setup", [])
        if not isinstance(setup, list) or len(setup) > 64:
            raise ValueError("setup must be a bounded ordered request list")
        normalized = validate_scenarios({"scenarios": [case]})[0]
        if normalized["id"] in ids:
            raise ValueError("duplicate scenario id")
        ids.add(normalized["id"])
        before = []
        for i, request in enumerate(setup):
            if not isinstance(request, dict) or set(request) - {
                "method",
                "path",
                "headers",
                "body",
                "expected_status",
                "expected_source_status",
            }:
                raise ValueError("unsupported setup request")
            request = dict(request, id=f"{normalized['id']}:setup:{i}")
            if not {"expected_status", "expected_source_status"} & request.keys():
                matches = [
                    r
                    for r in captured["routes"]
                    if r["method"] == request.get("method") and r["path"] == request.get("path")
                ]
                if len(matches) != 1:
                    raise ValueError("setup requires an expected status or captured literal route")
                request["expected_status"] = matches[0]["status"]
            before.append(request)
        groups.append(validate_scenarios({"scenarios": [*before, normalized]}))
    if sum(map(len, groups)) > MAX_SCENARIOS:
        raise ValueError("expanded scenarios exceed the shared replay limit")
    return groups


def replay_project(
    root: Path, output: Path, captured: dict[str, Any], command: str
) -> dict[str, Any]:
    from .capture import canonical, capture, digest
    from .render import render
    from .replay import _run, _snapshot, _source_python
    from .toolchain import ensure_go

    groups = scenario_groups(root, captured)
    dsn = os.environ.get("SANKA_GO_TARGET_TEST_DATABASE_URL", "")
    url = urlsplit(dsn)
    if (
        url.scheme not in {"postgres", "postgresql"}
        or not url.hostname
        or not url.path.strip("/")
        or url.fragment
    ):
        raise ValueError("requires explicit resettable SANKA_GO_TARGET_TEST_DATABASE_URL fixture")
    _ = url.port
    postgres = captured["drf_project"]["database"]["engine"] == "postgresql"
    source_dsn = os.environ.get("SANKA_GO_SOURCE_TEST_DATABASE_URL", "")
    if postgres and command == "verify":
        parsed = urlsplit(source_dsn)
        if (
            parsed.scheme not in {"postgres", "postgresql"}
            or not parsed.hostname
            or not parsed.path.strip("/")
            or parsed.fragment
        ):
            raise ValueError("requires explicit SANKA_GO_SOURCE_TEST_DATABASE_URL fixture")
        _ = parsed.port
    snapshot = _snapshot(output)
    if {
        "sanka_groups.json",
        "sanka_groups_observed.json",
        "sanka_drf_probe_test.go",
    } & snapshot.keys():
        raise ValueError("candidate conflicts with reserved replay artifacts")
    expected = render(captured)
    for name in ("go.mod", "go.sum", "contract.json"):
        if snapshot.get(name) != expected[name].encode():
            raise ValueError("candidate differs from the applied plan: " + name)
    if capture(root, captured["configuration"]) != captured:
        raise ValueError("source changed before replay")
    source_python = _source_python() if command == "verify" else None
    go, toolchain = ensure_go(root)
    # Preserve independent-case reset semantics; setup requests remain observed.
    source_groups, target_groups = [], []
    with tempfile.TemporaryDirectory(prefix="sanka-drf-go-") as directory:
        workspace = Path(directory)
        candidate, source = workspace / "candidate", workspace / "source"
        candidate.mkdir()
        source.mkdir()
        for name, contents in snapshot.items():
            path = candidate / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        inventory = captured["source_inventory"]["module_roles"]
        for name in sorted(set(inventory["application"] + inventory["migrations"])):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((root / name).read_bytes())
        (candidate / "sanka_groups.json").write_text(canonical([cases_document(g) for g in groups]))
        (candidate / "sanka_drf_probe_test.go").write_text(target_probe(captured))
        _run(
            [go, "test", "-mod=readonly", "-p=2", "./..."],
            candidate,
            timeout=900,
            environment=toolchain | {"DATABASE_URL": dsn},
        )
        target_groups = _observations(candidate / "sanka_groups_observed.json")
        if source_python:
            (workspace / "source_probe.py").write_text(SOURCE_PROBE)
            _run(
                [
                    source_python,
                    "-I",
                    str(workspace / "source_probe.py"),
                    str(source),
                    str(candidate / "contract.json"),
                    str(candidate / "sanka_groups.json"),
                    str(workspace / "source_observed.json"),
                ],
                workspace,
                timeout=180,
                environment={"SANKA_GO_SOURCE_TEST_DATABASE_URL": source_dsn}
                if postgres
                else {
                    captured["drf_project"]["database"]["environment"]: str(
                        workspace / "source.sqlite3"
                    )
                },
            )
            source_groups = _observations(workspace / "source_observed.json")
    failures: list[dict[str, Any]] = []
    if len(target_groups) != len(groups) or (source_python and len(source_groups) != len(groups)):
        raise ValueError("replay omitted scenario groups")
    for index, cases in enumerate(groups):
        target = validate_observations(
            {"schema": "sanka.http-observations/v1", "observations": target_groups[index]}, cases
        )
        actual = None
        if source_python:
            actual = validate_observations(
                {"schema": "sanka.http-observations/v1", "observations": source_groups[index]},
                cases,
            )
        comparison = compare(cases, target, actual)
        failures.extend(dict(step, group=index) for step in comparison["steps"] if step["problems"])
    if capture(root, captured["configuration"]) != captured or _snapshot(output) != snapshot:
        raise ValueError("source or generated files changed during replay")
    report = {
        "ok": not failures,
        "candidate": target_groups,
        "failures": failures,
        "scenarios": groups,
        "candidate_digest": digest({k: hashlib.sha256(v).hexdigest() for k, v in snapshot.items()}),
        "database": "isolated PostgreSQL source and target"
        if postgres
        else "isolated SQLite source and PostgreSQL target",
    }
    if source_python:
        report["source"] = source_groups
    return report


def _observations(path: Path) -> list[Any]:
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("observations exceed 16 MiB")
    result = json.loads(path.read_text())
    if not isinstance(result, list):
        raise ValueError("observations must contain scenario groups")
    return result


SOURCE_PROBE = r"""
import json, os, sys
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, unquote
import uuid
root, contract_file, cases_file, destination = map(Path, sys.argv[1:])
contract = json.loads(contract_file.read_text())
sys.path.insert(0, str(root))
os.environ['DJANGO_SETTINGS_MODULE'] = contract['drf_project']['settings_module']
if contract['drf_project'].get('secret_environment'):
    os.environ[contract['drf_project']['secret_environment']] = 'isolated-replay-only'
import django, rest_framework
if '.'.join(rest_framework.VERSION.split('.')[:2]) != contract['drf_project']['drf_version']:
    raise RuntimeError('Conventional DRF replay requires the captured DRF 3.18 source profile')
postgres = contract['drf_project']['database']['engine'] == 'postgresql'
admin = None
created = False
schema = 'sanka_drf_' + uuid.uuid4().hex
try:
    if postgres:
        import psycopg
        from psycopg import sql
        dsn = os.environ['SANKA_GO_SOURCE_TEST_DATABASE_URL']
        parsed = urlsplit(dsn)
        values = dict(NAME=unquote(parsed.path.lstrip('/')), USER=unquote(parsed.username or ''), PASSWORD=unquote(parsed.password or ''), HOST=parsed.hostname, PORT=str(parsed.port or 5432))
        for field, variable in contract['drf_project']['database']['environments'].items():
            os.environ[variable] = values[field]
        admin = psycopg.connect(dsn, autocommit=True)
        admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        created = True
        from django.conf import settings
        settings.DATABASES['default']['OPTIONS'] = {'options': '-c search_path=' + schema}
    django.setup()
    from django.core.management import call_command
    from django.db import connection
    from rest_framework.test import APIClient
    call_command('migrate', verbosity=0, interactive=False)
    result = []
    for group in json.loads(cases_file.read_text()):
        call_command('flush', verbosity=0, interactive=False)
        client = APIClient()
        observed = []
        for case in group['scenarios']:
            body = json.dumps(case['body']) if 'body' in case else ''
            response = client.generic(case['method'], case['path'], data=body, content_type='application/json', headers=case.get('headers', {}))
            tables, sequences = {}, {}
            with connection.cursor() as cursor:
                for model in contract['models']:
                    names = ','.join('"'+f['name']+'"' for f in model['fields'])
                    cursor.execute('SELECT '+names+' FROM "'+model['table']+'" ORDER BY id')
                    rows = []
                    for row in cursor.fetchall():
                        record = {}
                        for field, value in zip(model['fields'], row):
                            if value is not None and field['go_type'] == 'DecimalValue':
                                scale = int(field['sql_type'].split(',')[1][:-1])
                                value = format(Decimal(str(value)), '.'+str(scale)+'f')
                            elif field['go_type'] == 'bool': value = bool(value)
                            record[field['name']] = value
                        rows.append(record)
                    tables[model['table']] = rows
                    if postgres:
                        cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", [model['table']])
                        sequence = cursor.fetchone()[0]
                        cursor.execute(sql.SQL('SELECT last_value, is_called FROM {}').format(sql.Identifier(*sequence.split('.'))))
                        seq = cursor.fetchone()
                        sequences[model['table']] = [str(seq[0]), seq[1]]
                    else:
                        cursor.execute('SELECT seq FROM sqlite_sequence WHERE name=%s', [model['table']])
                        seq = cursor.fetchone()
                        sequences[model['table']] = [str(seq[0] if seq else 0), bool(seq and seq[0])]
            observed.append({'id':case['id'],'method':case['method'],'path':case['path'],'status':response.status_code,'media_type':response.headers.get('Content-Type','').split(';')[0],'body':json.loads(response.content) if response.content else None,'tables':tables,'sequences':sequences})
        result.append(observed)
    Path(destination).write_text(json.dumps(result))
    connection.close()
finally:
    if admin is not None:
        from django.db import connections
        connections.close_all()
        if created:
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        admin.close()

"""


def target_probe(captured: dict[str, Any]) -> str:
    target = captured["configuration"]["target_framework"]
    request = (
        'response,err:=app.Test(request);if err!=nil { t.Fatal(err) };raw,err:=io.ReadAll(response.Body);response.Body.Close();if err!=nil { t.Fatal(err) };status:=response.StatusCode;media:=response.Header.Get("Content-Type")'
        if target == "fiber"
        else 'response:=httptest.NewRecorder();app.ServeHTTP(response,request);raw:=response.Body.Bytes();status:=response.Code;media:=response.Header().Get("Content-Type")'
    )
    io_import = '"io";' if target == "fiber" else ""
    probe = r"""package backend
import ("bytes"; "context"; "encoding/json"; "net/http/httptest"; "os"; "strconv"; "strings"; "testing"; IO_IMPORT "github.com/jackc/pgx/v5/pgxpool")
func TestConventionalDRFReplay(t *testing.T) {
    ctx:=context.Background();dsn:=os.Getenv("DATABASE_URL")
    pool,err:=pgxpool.New(ctx,dsn);if err!=nil { t.Fatal(err) };defer pool.Close()
    data,err:=os.ReadFile("sanka_groups.json");if err!=nil { t.Fatal(err) }
    var groups []struct { Scenarios []struct { ID string `json:"id"`;Method string `json:"method"`;Path string `json:"path"`;Headers map[string]string `json:"headers"`;Body json.RawMessage `json:"body"` } `json:"scenarios"` }
    if err:=json.Unmarshal(data,&groups);err!=nil { t.Fatal(err) }
    if err:=Migrate(ctx,dsn,"up");err!=nil { t.Fatal(err) }
    defer func(){ if err:=Migrate(ctx,dsn,"down");err!=nil { t.Error(err) } }()
    results:=[][]map[string]any{}
    for _,group:=range groups {
        tables:=[]string{};for _,model:=range drfSchema.Models { tables=append(tables,quoted(model.Table)) }
        if _,err:=pool.Exec(ctx,"TRUNCATE "+strings.Join(tables,",")+" RESTART IDENTITY CASCADE");err!=nil { t.Fatal(err) }
        if _,err:=pool.Exec(ctx,"UPDATE migration_identity SET value=0");err!=nil { t.Fatal(err) }
        app:=NewApp(pool);observed:=[]map[string]any{}
        for _,item:=range group.Scenarios {
            request:=httptest.NewRequest(item.Method,"http://testserver"+item.Path,bytes.NewReader(item.Body))
            for key,value:=range item.Headers { request.Header.Set(key,value) }
            if len(item.Body)>0 { request.Header.Set("Content-Type","application/json") }
            REQUEST
            var body any
            if len(raw)>0 { decoder:=json.NewDecoder(bytes.NewReader(raw));decoder.UseNumber();if err:=decoder.Decode(&body);err!=nil { t.Fatal(err) } }
            snapshots:=map[string]any{};sequences:=map[string]any{}
            for _,model:=range drfSchema.Models {
                columns:=[]string{};for _,f:=range model.Fields { columns=append(columns,quoted(f.Name)+"::text") }
                rows,err:=pool.Query(ctx,"SELECT "+strings.Join(columns,",")+" FROM "+quoted(model.Table)+" ORDER BY id");if err!=nil { t.Fatal(err) }
                records:=[]map[string]any{}
                for rows.Next() {
                    values,err:=rows.Values();if err!=nil { t.Fatal(err) };record:=map[string]any{}
                    for i,f:=range model.Fields {
                        text:=values[i].(string)
                        switch f.GoType { case "int32","int64":record[f.Name]=json.Number(text);case "bool":record[f.Name]=text=="t" || text=="true";default:record[f.Name]=text }
                    };records=append(records,record)
                };if err:=rows.Err();err!=nil { t.Fatal(err) };rows.Close();snapshots[model.Table]=records
                var value int64;if err:=pool.QueryRow(ctx,"SELECT value FROM migration_identity WHERE table_name=$1",model.Table).Scan(&value);err!=nil { t.Fatal(err) }
                sequences[model.Table]=[]any{strconv.FormatInt(value,10),value!=0}
            }
            observed=append(observed,map[string]any{"id":item.ID,"method":item.Method,"path":item.Path,"status":status,"media_type":strings.Split(media,";")[0],"body":body,"tables":snapshots,"sequences":sequences})
        };results=append(results,observed)
    }
    output,err:=json.Marshal(results);if err!=nil { t.Fatal(err) }
    if err:=os.WriteFile("sanka_groups_observed.json",output,0600);err!=nil { t.Fatal(err) }
}
""".replace("IO_IMPORT", io_import).replace("REQUEST", request)

    if captured["drf_project"]["database"]["engine"] == "postgresql":
        probe = probe.replace(
            '        if _,err:=pool.Exec(ctx,"UPDATE migration_identity SET value=0");err!=nil { t.Fatal(err) }',
            "",
        )
        probe = probe.replace(
            'var value int64;if err:=pool.QueryRow(ctx,"SELECT value FROM migration_identity WHERE table_name=$1",model.Table).Scan(&value);err!=nil { t.Fatal(err) }\n                sequences[model.Table]=[]any{strconv.FormatInt(value,10),value!=0}',
            'var sequence string;if err:=pool.QueryRow(ctx,"SELECT pg_get_serial_sequence($1, \'id\')",model.Table).Scan(&sequence);err!=nil { t.Fatal(err) };var value int64;var called bool;if err:=pool.QueryRow(ctx,"SELECT last_value,is_called FROM "+sequence).Scan(&value,&called);err!=nil { t.Fatal(err) };sequences[model.Table]=[]any{strconv.FormatInt(value,10),called}',
        )
    return probe

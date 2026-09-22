# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""Rich PostgreSQL values must preserve model and wire semantics."""

from pathlib import Path

import pytest
from sanka_extension_python_to_golang.models import capture_models


def rich_models(framework: str) -> str:
    if framework == "drf":
        return """from django.db import models
class Entry(models.Model):
    id = models.UUIDField(primary_key=True)
    day = models.DateField()
    happened = models.DateTimeField()
    amount = models.DecimalField(max_digits=24, decimal_places=4)
    payload = models.JSONField(null=True)
    label = models.CharField(max_length=40, default="untitled", db_index=True)
    class Meta:
        app_label = "replay"
        db_table = "entries"
        constraints = [models.UniqueConstraint(fields=["day", "label"], name="entry_day_label")]
        indexes = [models.Index(fields=["happened", "label"], name="entry_time_label")]
"""
    return """from uuid import UUID
from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import Uuid, Date, DateTime, Numeric, String, UniqueConstraint, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
class Base(DeclarativeBase):
    pass
class Entry(Base):
    __tablename__ = "entries"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    day: Mapped[date] = mapped_column(Date)
    happened: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 4))
    payload: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    label: Mapped[str] = mapped_column(String(40), default="untitled", index=True)
    __table_args__ = (UniqueConstraint("day", "label", name="entry_day_label"), Index("entry_time_label", "happened", "label"))
"""


@pytest.mark.parametrize("framework", ["drf", "flask", "fastapi"])
def test_rich_model_contract(tmp_path: Path, framework: str) -> None:
    path = tmp_path / "models.py"
    path.write_text(rich_models(framework))
    model = capture_models(path, framework)[0]
    assert [f["sql_type"] for f in model["fields"]] == [
        "uuid",
        "date",
        "timestamp with time zone",
        "numeric(24,4)",
        "jsonb",
        "varchar(40)",
    ]
    assert not model["fields"][0]["auto"]
    assert model["fields"][-1]["default"] == "untitled"
    assert model["constraints"] == [
        {"name": "entry_day_label", "columns": ["day", "label"], "unique": True}
    ]
    assert model["indexes"] == [{"name": "entry_time_label", "columns": ["happened", "label"]}]


@pytest.mark.parametrize("framework", ["drf", "flask", "fastapi"])
def test_rich_schema_ddl(tmp_path: Path, framework: str) -> None:
    from sanka_extension_python_to_golang.database import render_database

    path = tmp_path / "models.py"
    path.write_text(rich_models(framework))
    files = render_database(
        {
            "models": capture_models(path, framework),
            "configuration": {"source_framework": framework, "schema_mode": "empty"},
        }
    )
    ddl = files["migrations/00001_initial.sql"]
    assert 'CONSTRAINT "entry_day_label" UNIQUE ("day", "label")' in ddl
    assert 'CREATE INDEX "entry_time_label" ON "entries" ("happened", "label")' in ddl
    assert '"id" uuid NOT NULL PRIMARY KEY' in ddl
    assert "DEFAULT" not in ddl  # Python defaults are not database defaults.
    assert "values.go" in files


def test_rich_pgx_codecs(tmp_path: Path) -> None:
    import os
    import subprocess

    from test_golang_schema import generate

    if os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("set SANKA_GO_TESTS=1 for native codecs")
    output = generate(tmp_path, "fastapi", "fiber", model_text=rich_models("fastapi"))
    (output / "values_test.go").write_text(r"""package backend
import (
    "encoding/json"
    "testing"
    "github.com/jackc/pgx/v5/pgtype"
)
func TestRichValues(t *testing.T) {
    m := pgtype.NewMap()
    uuid := UUIDValue("12345678-1234-5678-9abc-123456789abc")
    day := DateValue("2024-02-29")
    timestamp := TimestampValue("2024-02-29T12:34:56.123456+00:00")
    amount := DecimalValue("12345678901234567890.1234")
    var gotUUID UUIDValue
    var gotDay DateValue
    var gotTimestamp TimestampValue
    var gotAmount DecimalValue
    cases := []struct{ oid uint32; value, target any }{
        {pgtype.UUIDOID, uuid, &gotUUID},
        {pgtype.DateOID, day, &gotDay},
        {pgtype.TimestamptzOID, timestamp, &gotTimestamp},
        {pgtype.NumericOID, amount, &gotAmount},
    }
    for _, format := range []int16{pgtype.TextFormatCode, pgtype.BinaryFormatCode} {
        for _, c := range cases {
            data, err := m.Encode(c.oid, format, c.value, nil)
            if err != nil { t.Fatal(err) }
            if err = m.Scan(c.oid, format, data, c.target); err != nil { t.Fatal(err) }
            expected, _ := json.Marshal(c.value)
            actual, _ := json.Marshal(c.target)
            if string(expected) != string(actual) { t.Fatalf("%s != %s", expected, actual) }
        }
    }
    if !validDecimal(string(amount), 24, 4) { t.Fatal("lost exact decimal") }
    for _, invalid := range []string{"1.23456", "1e2", "-0.0000", "NaN", "01.0000", "123456789012345678901.0000"} {
        if validDecimal(invalid, 24, 4) { t.Fatal(invalid) }
    }
    for _, invalid := range []string{`"2023-02-29"`, `"0000-01-01"`, `null`} {
        if json.Unmarshal([]byte(invalid), &gotDay) == nil { t.Fatal(invalid) }
    }
    if json.Unmarshal([]byte(`"2024-02-29T12:34:56.1234567+00:00"`), &gotTimestamp) == nil { t.Fatal("truncated precision") }
}
""")
    result = subprocess.run(
        ["go", "test", "-p=2", "-mod=readonly", "./..."],
        cwd=output,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def rich_app(framework: str) -> str:
    from sanka_extension_python_to_golang.values import SOURCE_VALIDATORS

    imports = """from uuid import UUID
from datetime import date, datetime, timezone
from decimal import Decimal
from re import fullmatch
from models import Entry
"""
    if framework == "drf":
        header = """from django.urls import path
from rest_framework.decorators import api_view, authentication_classes, permission_classes, renderer_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.renderers import JSONRenderer
"""
        decorator = "@api_view(['POST'])\n@authentication_classes([])\n@permission_classes([AllowAny])\n@renderer_classes([JSONRenderer])"
        signature = "request"
        data = "request.data"
        failure = 'return Response({"error": "invalid request body"}, status=400)'
    else:
        header = """from os import environ
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
"""
        header += (
            "from flask import Flask, request, jsonify\napp = Flask(__name__)\n"
            if framework == "flask"
            else "from fastapi import FastAPI, HTTPException\napp = FastAPI()\n"
        )
        header += 'engine = create_engine(environ["DATABASE_URL"])\n'
        decorator = (
            '@app.post("/entries")'
            if framework == "flask"
            else '@app.post("/entries", status_code=201)'
        )
        signature = "" if framework == "flask" else "data: dict"
        data = "data"
        failure = (
            'return jsonify({"error": "invalid request body"}), 400'
            if framework == "flask"
            else 'raise HTTPException(status_code=400, detail="invalid request body")'
        )
    validation = f"""if (
    (type({data}) is not dict)
    or (set({data}) - {{'id', 'day', 'happened', 'amount', 'payload', 'label'}})
    or ('id' not in {data})
    or (not valid_uuid({data}['id']))
    or ('day' not in {data})
    or (not valid_date({data}['day']))
    or ('happened' not in {data})
    or (not valid_timestamp({data}['happened']))
    or ('amount' not in {data})
    or (not valid_decimal({data}['amount'], 24, 4))
    or ('payload' in {data} and ({data}['payload'] is not None and (not valid_json({data}['payload']))))
    or ('label' in {data} and (type({data}['label']) is not str))
):
    {failure}
"""
    values = f"id=UUID({data}['id']), day=date.fromisoformat({data}['day']), happened=datetime.fromisoformat({data}['happened']), amount=Decimal({data}['amount']), payload={data}.get('payload'), label={data}.get('label', 'untitled')"
    response = """{'id': str(item.id), 'day': item.day.isoformat(), 'happened': item.happened.astimezone(timezone.utc).isoformat(timespec="microseconds"), 'amount': format(item.amount, 'f'), 'payload': item.payload, 'label': item.label}"""
    if framework == "drf":
        body = (
            validation
            + f"item = Entry.objects.create({values})\nreturn Response({response}, status=201)\n"
        )
    else:
        body = ("data = request.get_json()\n" if framework == "flask" else "") + validation
        result = f"jsonify({response}), 201" if framework == "flask" else response
        body += f"""with Session(engine) as session:
    item = Entry({values})
    session.add(item)
    session.commit()
    session.refresh(item)
    return {result}
"""
    return (
        imports
        + header
        + SOURCE_VALIDATORS
        + "\n"
        + decorator
        + "\ndef create("
        + signature
        + "):\n"
        + "\n".join("    " + line for line in body.splitlines())
        + ("\nurlpatterns = [path('entries', create)]\n" if framework == "drf" else "\n")
    )


@pytest.mark.parametrize("framework", ["drf", "flask", "fastapi"])
def test_rich_create_capture(tmp_path: Path, framework: str) -> None:
    from test_golang_schema import generate

    generate(
        tmp_path,
        framework,
        "fiber",
        model_text=rich_models(framework),
        app_source=rich_app(framework),
    )


@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_rich_decoder(tmp_path: Path, target: str) -> None:
    import os
    import subprocess

    from test_golang_schema import generate

    if os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("set SANKA_GO_TESTS=1 for native decoding")
    output = generate(
        tmp_path,
        "fastapi",
        target,
        model_text=rich_models("fastapi"),
        app_source=rich_app("fastapi"),
    )
    (output / "rich_decoder_test.go").write_text(r"""package backend
import (
    "testing"
    "strings"
)
func TestRichDecoder(t *testing.T) {
    body := `{"id":"12345678-1234-5678-9abc-123456789abc","day":"2024-02-29","happened":"2024-02-29T12:34:56.123456+00:00","amount":"12345678901234567890.1234","payload":{"nested":[1,true,null]}}`
    item, _, err := decodeEntry([]byte(body), false)
    if err != nil { t.Fatal(err) }
    if item.Label != "untitled" || item.Amount != "12345678901234567890.1234" { t.Fatalf("%+v", item) }
    partial, seen, err := decodeEntry([]byte("{}"), true)
    if err != nil || len(seen) != 0 || partial.Label != "" { t.Fatal("PATCH applied defaults") }
    for _, invalid := range []string{
        strings.Replace(body, "12345678901234567890.1234", "1.23456", 1),
        strings.Replace(body, "2024-02-29", "2023-02-29", 1),
        strings.Replace(body, "[1,true,null]", "[1.5]", 1),
        strings.Replace(body, "[1,true,null]", "[9007199254740992]", 1),
    } {
        if _, _, err := decodeEntry([]byte(invalid), false); err == nil { t.Fatal(invalid) }
    }
}
""")
    result = subprocess.run(
        ["go", "test", "-p=2", "-mod=readonly", "./..."],
        cwd=output,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def rich_backend(framework: str) -> str:
    import ast
    import copy
    import textwrap

    tree = ast.parse(rich_app(framework))
    create = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "create"
    )
    validation = copy.deepcopy(create.body[1 if framework == "flask" else 0])
    response = """{'id': str(item.id), 'day': item.day.isoformat(), 'happened': item.happened.astimezone(timezone.utc).isoformat(timespec="microseconds"), 'amount': format(item.amount, 'f'), 'payload': item.payload, 'label': item.label}"""
    data = "request.data" if framework == "drf" else "data"
    failure = {
        "drf": 'return Response({"error": "not found"}, status=404)',
        "flask": 'return jsonify({"error": "not found"}), 404',
        "fastapi": 'raise HTTPException(status_code=404, detail="not found")',
    }[framework]
    invalid = failure.replace("not found", "invalid lookup").replace("404", "400")
    prefix = f"if not valid_uuid(id):\n    {invalid}\nid = UUID(id)\n"
    get = (
        "item = Entry.objects.filter(id=id).first()"
        if framework == "drf"
        else "item = session.get(Entry, id)"
    )
    get += f"\nif item is None:\n    {failure}\n"
    result = (
        f"return Response({response})"
        if framework == "drf"
        else f"return jsonify({response})"
        if framework == "flask"
        else f"return {response}"
    )
    values = {
        "id": f"UUID({data}['id'])",
        "day": f"date.fromisoformat({data}['day'])",
        "happened": f"datetime.fromisoformat({data}['happened'])",
        "amount": f"Decimal({data}['amount'])",
        "payload": f"{data}.get('payload')",
        "label": f"{data}.get('label', 'untitled')",
    }
    bindings = []
    for method in ("get", "put", "patch", "delete"):
        name = method + "_entry"
        body = prefix
        if method in {"put", "patch"}:
            if framework == "flask":
                body += "data = request.get_json()\n"
            check = ast.unparse(validation)
            if method == "patch":
                # Preserve each independently written predicate, adding presence guards.
                tests = validation.test.values
                kept = tests[:2]
                for field, predicate in zip(
                    ("id", "day", "happened", "amount"), tests[3:10:2], strict=True
                ):
                    kept.append(
                        ast.parse(
                            f"'{field}' in {data} and ({ast.unparse(predicate)})", mode="eval"
                        ).body
                    )
                partial = copy.deepcopy(validation)
                partial.test.values = kept + tests[10:]
                check = ast.unparse(partial)
            if framework == "drf":
                parsed_check = ast.parse(check).body[0]
                # First predicate checks keys; remove primary from allowed set.
                allowed = parsed_check.test.values[1].right
                allowed.elts = [item for item in allowed.elts if item.value != "id"]
                skip = 1 if method == "patch" else 2
                parsed_check.test.values = (
                    parsed_check.test.values[:2] + parsed_check.test.values[2 + skip :]
                )
                check = ast.unparse(parsed_check)
            body += check + "\n"
        operation = get
        if method == "delete":
            operation += (
                "item.delete()\nreturn Response(status=204)"
                if framework == "drf"
                else "session.delete(item)\nsession.commit()\n"
                + ('return "", 204' if framework == "flask" else "")
            )
        else:
            if method in {"put", "patch"}:
                for field, expression in values.items():
                    if framework == "drf" and field == "id":
                        continue
                    if method == "patch":
                        expression = expression.replace(
                            f"{data}.get('payload')", f"{data}['payload']"
                        ).replace(f"{data}.get('label', 'untitled')", f"{data}['label']")
                        operation += f"if '{field}' in {data}:\n    item.{field} = {expression}\n"
                    else:
                        operation += f"item.{field} = {expression}\n"
                if framework == "drf":
                    update = (
                        f"list({data})"
                        if method == "patch"
                        else repr([field for field in values if field != "id"])
                    )
                    operation += f"item.save(update_fields={update})\n"
                else:
                    operation += "session.commit()\nsession.refresh(item)\n"
            operation += result
        body += (
            operation
            if framework == "drf"
            else "with Session(engine) as session:\n" + textwrap.indent(operation, "    ")
        )
        if framework == "drf":
            decorators = (
                "@api_view(['"
                + method.upper()
                + "'])\n@authentication_classes([])\n@permission_classes([AllowAny])\n@renderer_classes([JSONRenderer])"
            )
            signature = "request, id"
            bindings.append(f"path('entries/{method}/<str:id>', {name})")
        else:
            route = "/entries/<id>" if framework == "flask" else "/entries/{id}"
            status = ", status_code=204" if method == "delete" and framework == "fastapi" else ""
            decorators = f"@app.{method}({route!r}{status})"
            signature = (
                "id"
                if framework == "flask"
                else "id: str" + (", data: dict" if method in {"put", "patch"} else "")
            )
        tree.body.extend(
            ast.parse(
                decorators + f"\ndef {name}({signature}):\n" + textwrap.indent(body, "    ")
            ).body
        )
    fields = ["id", "day", "happened", "amount", "payload", "label"]
    projected = response
    for field in fields:
        projected = projected.replace("item." + field, f"row[{field!r}]")
    if framework == "drf":
        collection = f"[{projected} for row in Entry.objects.order_by('id').values({', '.join(repr(field) for field in fields)})[:100]]"
        list_function = (
            "@api_view(['GET'])\n@authentication_classes([])\n@permission_classes([AllowAny])\n@renderer_classes([JSONRenderer])\ndef list_entries(request):\n    return Response("
            + collection
            + ")"
        )
        bindings.append("path('entry-list', list_entries)")
    else:
        collection = f"[{projected} for row in session.execute(select({', '.join('Entry.' + field for field in fields)}).order_by(Entry.id).limit(100)).mappings()]"
        if framework == "flask":
            collection = f"jsonify({collection})"
        list_function = (
            '@app.get("/entry-list")\ndef list_entries():\n    with Session(engine) as session:\n        return '
            + collection
        )
        tree.body.insert(0, ast.parse("from sqlalchemy import select").body[0])
    tree.body.extend(ast.parse(list_function).body)
    if framework == "drf":
        assignment = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "urlpatterns"
        )
        tree.body.remove(assignment)
        tree.body.extend(
            ast.parse("urlpatterns = [path('entries', create), " + ", ".join(bindings) + "]").body
        )
        module = "django.db"
    else:
        module = "sqlalchemy.exc"
    tree.body.insert(0, ast.parse(f"from {module} import IntegrityError").body[0])
    conflict = failure.replace("not found", "integrity conflict").replace("404", "409")
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in {
            "create",
            "put_entry",
            "patch_entry",
            "delete_entry",
        }:
            wrapper = ast.parse("try:\n    pass\nexcept IntegrityError:\n    " + conflict).body[0]
            wrapper.body = node.body
            node.body = [wrapper]
    return ast.unparse(ast.fix_missing_locations(tree))


@pytest.mark.parametrize("framework", ["drf", "flask", "fastapi"])
@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_rich_backend_capture(tmp_path: Path, framework: str, target: str) -> None:
    import os
    import subprocess

    from test_golang_schema import generate

    output = generate(
        tmp_path,
        framework,
        target,
        model_text=rich_models(framework),
        app_source=rich_backend(framework),
    )
    if os.getenv("SANKA_GO_TESTS") == "1":
        result = subprocess.run(
            ["go", "test", "-p=2", "-mod=readonly", "./..."],
            cwd=output,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr


def rich_scenarios(framework: str) -> list[dict]:
    key = "12345678-1234-5678-9abc-123456789abc"
    body = {
        "id": key,
        "day": "2024-02-29",
        "happened": "2024-02-29T12:34:56.123456+00:00",
        "amount": "12345678901234567890.1234",
        "payload": {"nested": [1, True, None]},
    }

    def case(name, method, payload=None, status=200):
        path = (
            "/entries"
            if method == "POST"
            else f"/entries/{method.lower()}/{key}"
            if framework == "drf"
            else f"/entries/{key}"
        )
        result = {"id": name, "method": method, "path": path, "expected_status": status}
        if payload is not None:
            result["body"] = payload
        return result

    return [
        case("missing", "POST", {}, 400),
        case("rounding-rejected", "POST", body | {"amount": "1.23456"}, 400),
        case("invalid-date", "POST", body | {"day": "2023-02-29"}, 400),
        case("json-number-rejected", "POST", body | {"payload": 9007199254740992}, 400),
        case("create", "POST", body, 201),
        case("duplicate-pk", "POST", body, 409),
        case(
            "duplicate-composite",
            "POST",
            body | {"id": "22345678-1234-5678-9abc-123456789abc"},
            409,
        ),
        case("lookup", "GET"),
        {"id": "list", "method": "GET", "path": "/entry-list", "expected_status": 200},
        case("partial-null", "PATCH", {"payload": None}),
        case("partial-absent", "PATCH", {}),
        case("partial-decimal", "PATCH", {"amount": "0.0001"}),
        case(
            "replace-default",
            "PUT",
            {
                k: v
                for k, v in (body | {"day": "2024-03-01"}).items()
                if framework != "drf" or k != "id"
            },
        ),
        case("delete", "DELETE", status=204),
        case("delete-again", "DELETE", status=404),
        case("recreate", "POST", body, 201),
    ]


@pytest.mark.parametrize("framework", ["drf", "flask", "fastapi"])
def test_original_rich_validation(tmp_path: Path, framework: str) -> None:
    import json
    import os
    import subprocess
    import sys

    from sanka_extension_python_to_golang.replay import SOURCE_PROBE

    (tmp_path / "app.py").write_text(rich_backend(framework))
    (tmp_path / "models.py").write_text(rich_models(framework))
    script = (
        SOURCE_PROBE[: SOURCE_PROBE.index("observed = []")]
        + """
for case in json.loads(routes):
    if framework == "drf":
        response = client.post(case['path'], data=json.dumps(case['body']), content_type="application/json")
    else:
        response = client.post(case['path'], json=case['body'])
    assert response.status_code == case['expected_status'], response
"""
    )
    run = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            script,
            framework,
            str(tmp_path / "app.py"),
            json.dumps(rich_scenarios(framework)[:4]),
            str(tmp_path / "unused.json"),
            str(tmp_path / "models.py"),
            "0",
        ],
        env=os.environ
        | {"DATABASE_URL": "postgresql+psycopg://fixture:fixture@127.0.0.1:1/unused"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.parametrize(
    "framework",
    [
        "drf",
        "flask",
        "fastapi",
        "flask-json-null",
        "fastapi-json-null",
        "fastapi-small-decimal",
        "drf-relational",
        "flask-relational",
        "fastapi-relational",
    ],
)
@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_rich_database_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, framework: str, target: str
) -> None:
    import dataclasses
    import json
    import os
    import uuid

    if not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN") or os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("requires isolated PostgreSQL fixtures and Go")
    json_null = framework.endswith("-json-null")
    relational = framework.endswith("-relational")
    small_decimal = framework.endswith("-small-decimal")
    framework = (
        framework.removesuffix("-json-null")
        .removesuffix("-relational")
        .removesuffix("-small-decimal")
    )
    if (json_null or relational or small_decimal) and target != "fiber":
        pytest.skip("JSON storage semantics use the shared pgx path")
    import psycopg
    from psycopg import sql
    from sanka_extension_python_to_golang.adapter import handle
    from test_golang_schema import generate, schema_dsn
    from test_python_to_golang import request

    app, models = (
        rich_relational(framework)
        if relational
        else (rich_backend(framework), rich_models(framework))
    )
    if json_null:
        models = models.replace("none_as_null=True", "none_as_null=False")
    scenarios = rich_scenarios(framework)
    if small_decimal:
        models = models.replace("Numeric(24, 4)", "Numeric(24, 8)")
        app = app.replace("24, 4", "24, 8")
        for case in scenarios:
            body = case.get("body", {})
            if body.get("amount") == "12345678901234567890.1234":
                body["amount"] = "1234567890123456.12340000"
            elif body.get("amount") == "0.0001":
                body["amount"] = "0.00000001"
    if relational:
        entry = scenarios[4]["body"]

        def create_case(name, key, day, label, status):
            return {
                "id": name,
                "method": "POST",
                "path": "/entries",
                "body": {"entry": entry | {"id": key, "day": day}, "line": {"label": label}},
                "expected_status": status,
            }

        scenarios = [
            create_case("create-related", entry["id"], entry["day"], "first", 201),
            create_case(
                "rollback-parent",
                "22345678-1234-5678-9abc-123456789abc",
                "2024-03-01",
                "first",
                409,
            ),
            create_case(
                "recover-parent",
                "22345678-1234-5678-9abc-123456789abc",
                "2024-03-01",
                "second",
                201,
            ),
        ]
    (tmp_path / "sanka-verify.json").write_text(json.dumps({"scenarios": scenarios}))
    generate(tmp_path, framework, target, model_text=models, app_source=app)
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    schemas = ["rich_" + uuid.uuid4().hex for _ in range(2)]
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            for schema in schemas:
                admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            source, target_dsn = [schema_dsn(dsn, schema) for schema in schemas]
            if framework != "drf":
                source = source.replace("postgresql://", "postgresql+psycopg://", 1)
            monkeypatch.setenv("SANKA_GO_SOURCE_TEST_DATABASE_URL", source)
            monkeypatch.setenv("SANKA_GO_TARGET_TEST_DATABASE_URL", target_dsn)
            req = request(tmp_path, framework, target)
            result = handle(
                dataclasses.replace(
                    req,
                    command="verify",
                    configuration=req.configuration | {"database_layer": "pgx"},
                )
            )
            assert result.outcome == "success", result.error
            assert result.data["ok"]
            assert result.data["source"] == result.data["candidate"]
            if not relational:
                observed = next(row for row in result.data["source"] if row["id"] == "partial-null")
                assert observed["tables"]["entries"][0]["payload"] == {
                    "value": None,
                    "sql_null": not json_null,
                }
        finally:
            for schema in schemas:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )


@pytest.mark.parametrize(
    "before,after",
    [
        ("Numeric(24, 4)", "Numeric(24, 25)"),
        ("Numeric(24, 4)", "Numeric(24, 4, asdecimal=False)"),
        ("DateTime(timezone=True)", "DateTime"),
        ('default="untitled"', "default=lambda: 'random'"),
        ('"entry_time_label"', '"entry_day_label"'),
        ('"happened", "label"))', '"missing", "label"))'),
        ("Uuid, primary_key=True", "Uuid, primary_key=True, autoincrement=True"),
    ],
)
def test_rich_model_rejects_unqualified_options(tmp_path: Path, before: str, after: str) -> None:
    path = tmp_path / "models.py"
    original = rich_models("fastapi")
    assert before in original
    path.write_text(original.replace(before, after))
    with pytest.raises((ValueError, TypeError)):
        capture_models(path, "fastapi")


@pytest.mark.parametrize("framework", ["drf", "flask", "fastapi"])
def test_rich_adoption(tmp_path: Path, framework: str) -> None:
    import os
    import subprocess
    import sys
    import uuid

    if not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN") or os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("requires isolated PostgreSQL fixtures and Go")
    import psycopg
    from psycopg import sql
    from test_golang_schema import SOURCE_DDL, generate, schema_dsn

    output = generate(
        tmp_path,
        framework,
        "fiber",
        schema_mode="adopt-existing",
        model_text=rich_models(framework)
        .replace("models.DateField()", "models.DateField(null=True)")
        .replace("day: Mapped[date]", "day: Mapped[date | None]"),
    )
    binary = tmp_path / "adopt"
    build = subprocess.run(
        ["go", "build", "-mod=readonly", "-p=2", "-o", str(binary), "./cmd/migrate"],
        cwd=output,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert build.returncode == 0, build.stderr
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    schema = "rich_adopt_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            url = schema_dsn(dsn, schema)
            setup = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    SOURCE_DDL.replace("module.Widget", "module.Entry"),
                    framework,
                    str(tmp_path / "models.py"),
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert setup.returncode == 0, setup.stderr

            def migrate(direction, success):
                result = subprocess.run(
                    [str(binary), direction],
                    env=os.environ | {"DATABASE_URL": url},
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                assert (result.returncode == 0) is success, result.stderr

            migrate("up", True)
            migrate("down", True)
            with psycopg.connect(url, autocommit=True) as connection:
                connection.execute("ALTER TABLE entries ALTER COLUMN amount TYPE numeric(25,4)")
                migrate("up", False)
                connection.execute("ALTER TABLE entries ALTER COLUMN amount TYPE numeric(24,4)")
                connection.execute("DROP INDEX entry_time_label")
                migrate("up", False)
                connection.execute("CREATE INDEX entry_time_label ON entries (label, happened)")
                migrate("up", False)
                connection.execute("DROP INDEX entry_time_label")
                connection.execute("CREATE INDEX entry_time_label ON entries (happened, label)")
                migrate("up", True)
                migrate("down", True)
                index_name = connection.execute("""SELECT i.relname FROM pg_index x
                    JOIN pg_class i ON i.oid=x.indexrelid
                    JOIN pg_class t ON t.oid=x.indrelid
                    JOIN pg_namespace n ON n.oid=t.relnamespace
                    JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=x.indkey[0]
                    JOIN pg_opclass oc ON oc.oid=x.indclass[0]
                    WHERE n.nspname=current_schema() AND t.relname='entries'
                      AND x.indnatts=1 AND a.attname='label' AND oc.opcdefault
                      AND NOT x.indisunique""").fetchone()[0]
                connection.execute(sql.SQL("DROP INDEX {}").format(sql.Identifier(index_name)))
                migrate("up", False)
                connection.execute("CREATE INDEX rich_label_idx ON entries (label)")
                migrate("up", True)
                migrate("down", True)
                if connection.info.server_version >= 150000:
                    connection.execute("ALTER TABLE entries DROP CONSTRAINT entry_day_label")
                    connection.execute(
                        "ALTER TABLE entries ADD CONSTRAINT entry_day_label UNIQUE NULLS NOT DISTINCT (day, label)"
                    )
                    migrate("up", False)
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def rich_relational(framework: str) -> tuple[str, str]:
    import ast
    import textwrap

    models = rich_models(framework)
    if framework == "drf":
        models += """class Line(models.Model):
    id = models.BigAutoField(primary_key=True)
    entry = models.ForeignKey(Entry, on_delete=models.DO_NOTHING)
    label = models.CharField(max_length=40, unique=True)
    class Meta:
        app_label = "replay"
        db_table = "lines"
"""
    else:
        models = (
            "from sqlalchemy import BigInteger, ForeignKey\n"
            + models
            + """class Line(Base):
    __tablename__ = "lines"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entry_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("entries.id"))
    label: Mapped[str] = mapped_column(String(40), unique=True)
"""
        )
    tree = ast.parse(
        rich_app(framework).replace("from models import Entry", "from models import Entry, Line")
    )
    create = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "create"
    )
    data = "request.data" if framework == "drf" else "data"
    validation = ast.unparse(create.body[1 if framework == "flask" else 0]).replace(
        data, data + "['entry']"
    )
    failure = {
        "drf": 'return Response({"error": "invalid request body"}, status=400)',
        "flask": 'return jsonify({"error": "invalid request body"}), 400',
        "fastapi": 'raise HTTPException(status_code=400, detail="invalid request body")',
    }[framework]
    prefix = "data = request.get_json()\n" if framework == "flask" else ""
    prefix += f"if type({data}) is not dict or set({data}) != {{'entry', 'line'}}:\n    {failure}\n"
    prefix += validation + "\n"
    prefix += f"""if ((type({data}['line']) is not dict)
    or (set({data}['line']) - {{'label'}})
    or ('label' not in {data}['line'])
    or (type({data}['line']['label']) is not str)):
    {failure}
"""
    # Reuse the independently written constructor expression in the fixture.
    assignment = create.body[1] if framework == "drf" else create.body[-1].body[0]
    constructor = ast.unparse(assignment.value).replace(data, data + "['entry']")
    entry = "entry = " + constructor
    child = f"line = Line{'.objects.create' if framework == 'drf' else ''}(entry_id=entry.id, label={data}['line']['label'])"
    snapshot = "result = {'id': line.id, 'entry_id': str(line.entry_id), 'label': line.label}"
    if framework == "drf":
        body = (
            prefix
            + "with transaction.atomic():\n"
            + textwrap.indent(entry + "\n" + child + "\n" + snapshot, "    ")
            + "\nreturn Response(result, status=201)"
        )
        imports = "from django.db import transaction, IntegrityError"
    else:
        operations = (
            entry
            + "\nsession.add(entry)\nsession.flush()\n"
            + child
            + "\nsession.add(line)\nsession.flush()\n"
            + snapshot
        )
        body = (
            prefix
            + "with Session(engine) as session:\n    with session.begin():\n"
            + textwrap.indent(operations, "        ")
            + "\n"
            + ("return jsonify(result), 201" if framework == "flask" else "return result")
        )
        imports = "from sqlalchemy.exc import IntegrityError"
    conflict = failure.replace("invalid request body", "integrity conflict").replace("400", "409")
    create.body = ast.parse(
        "try:\n" + textwrap.indent(body, "    ") + "\nexcept IntegrityError:\n    " + conflict
    ).body
    tree.body.insert(0, ast.parse(imports).body[0])
    return ast.unparse(ast.fix_missing_locations(tree)), models


@pytest.mark.parametrize("framework", ["drf", "flask", "fastapi"])
def test_rich_foreign_key_transaction(tmp_path: Path, framework: str) -> None:
    import os
    import subprocess

    from test_golang_schema import generate

    app, models = rich_relational(framework)
    output = generate(tmp_path, framework, "fiber", model_text=models, app_source=app)
    migration = (output / "migrations/00001_initial.sql").read_text()
    assert '"entry_id" uuid NOT NULL REFERENCES "entries" ("id")' in migration
    if os.getenv("SANKA_GO_TESTS") == "1":
        result = subprocess.run(
            ["go", "test", "-p=2", "-mod=readonly", "./..."],
            cwd=output,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr


def test_nullable_sqlalchemy_default_is_not_silently_changed(tmp_path: Path) -> None:
    path = tmp_path / "models.py"
    path.write_text(
        rich_models("fastapi").replace("label: Mapped[str]", "label: Mapped[str | None]")
    )
    with pytest.raises(ValueError, match=r"nullable.*default"):
        capture_models(path, "fastapi")


def test_adoption_requires_field_indexes(tmp_path: Path) -> None:
    from sanka_extension_python_to_golang.database import render_database

    path = tmp_path / "models.py"
    path.write_text(rich_models("fastapi"))
    migration = render_database(
        {
            "models": capture_models(path, "fastapi"),
            "configuration": {"source_framework": "fastapi", "schema_mode": "adopt-existing"},
        }
    )["migrations/00001_initial.sql"]
    # Require the scalar index shape as well as the named multi-column index.
    assert "'[\"label\"]'::jsonb" in migration


def test_adoption_checks_unique_null_semantics(tmp_path: Path) -> None:
    from sanka_extension_python_to_golang.database import render_database

    path = tmp_path / "models.py"
    path.write_text(rich_models("fastapi"))
    migration = render_database(
        {
            "models": capture_models(path, "fastapi"),
            "configuration": {"source_framework": "fastapi", "schema_mode": "adopt-existing"},
        }
    )["migrations/00001_initial.sql"]
    assert "indnullsnotdistinct" in migration


def test_small_decimal_wire_is_fixed_point() -> None:
    from decimal import Decimal
    from types import SimpleNamespace

    from sanka_extension_python_to_golang.values import output_value

    field = {"go_type": "DecimalValue", "nullable": False}
    expression = output_value(field, "item.amount")
    for value in ("0.00000001", "0.00000000", "-0.00000010"):
        item = SimpleNamespace(amount=Decimal(value))
        assert eval(expression, {"item": item}) == value


@pytest.mark.parametrize("framework", ["drf", "flask", "fastapi"])
def test_rich_read_only_needs_no_unused_validators(tmp_path: Path, framework: str) -> None:
    import ast

    from test_golang_schema import generate

    tree = ast.parse(rich_backend(framework))
    tree.body = [
        node
        for node in tree.body
        if not isinstance(node, ast.FunctionDef) or node.name == "list_entries"
    ]
    if framework == "drf":
        for node in tree.body:
            if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "urlpatterns":
                node.value = ast.parse("[path('entry-list', list_entries)]", mode="eval").body
    generate(
        tmp_path,
        framework,
        "fiber",
        model_text=rich_models(framework),
        app_source=ast.unparse(ast.fix_missing_locations(tree)),
    )

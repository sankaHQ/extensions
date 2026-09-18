# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""Bounded cross-framework pgx write-generation contract."""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path
from textwrap import indent

import pytest
from sanka_extension_python_to_golang.capture import TARGETS, capture, configuration
from sanka_extension_python_to_golang.render import render
from sanka_extension_python_to_golang.replay import replay
from test_golang_schema import generate, model_source, schema_dsn


def widget_validation(data: str, partial: bool, error: str) -> str:
    fields = [
        ("name", "str", False),
        ("count", "int", False),
        ("enabled", "bool", False),
        ("note", "str", True),
    ]
    conditions = [
        f"type({data}) is not dict",
        f"set({data}) - {{'name', 'count', 'enabled', 'note'}}",
    ]
    for name, kind, nullable in fields:
        key, value = repr(name), f"{data}[{name!r}]"
        if not partial and not nullable:
            conditions.append(f"{key} not in {data}")
        invalid = f"type({value}) is not {kind}"
        if name == "count":
            invalid += f" or not -2147483648 <= {value} <= 2147483647"
        if nullable:
            invalid = f"{value} is not None and ({invalid})"
        if partial or nullable:
            invalid = f"{key} in {data} and ({invalid})"
        conditions.append(invalid)
    joined = "\n        or ".join(f"({condition})" for condition in conditions)
    return f"if (\n        {joined}\n    ):\n        {error}\n"


def flask_write_source() -> str:
    text = """from flask import Flask, jsonify, request
from models import Widget
from os import environ
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
engine = create_engine(environ["DATABASE_URL"])
app = Flask(__name__)
@app.post("/widgets")
def create_widget():
    data = request.get_json()
    with Session(engine) as session:
        item = Widget(name=data["name"], count=data["count"], enabled=data["enabled"], note=data.get("note"))
        session.add(item)
        session.commit()
        session.refresh(item)
        return jsonify({"id": item.id, "name": item.name, "count": item.count, "enabled": item.enabled, "note": item.note}), 201
@app.patch("/widgets/<int:id>")
def patch_widget(id):
    data = request.get_json()
    with Session(engine) as session:
        item = session.get(Widget, id)
        if item is None:
            return jsonify({"error": "not found"}), 404
        if "name" in data:
            item.name = data["name"]
        if "count" in data:
            item.count = data["count"]
        if "enabled" in data:
            item.enabled = data["enabled"]
        if "note" in data:
            item.note = data["note"]
        session.commit()
        session.refresh(item)
        return jsonify({"id": item.id, "name": item.name, "count": item.count, "enabled": item.enabled, "note": item.note})
@app.put("/widgets/<int:id>")
def replace_widget(id):
    data = request.get_json()
    with Session(engine) as session:
        item = session.get(Widget, id)
        if item is None:
            return jsonify({"error": "not found"}), 404
        item.name = data["name"]
        item.count = data["count"]
        item.enabled = data["enabled"]
        item.note = data.get("note")
        session.commit()
        session.refresh(item)
        return jsonify({"id": item.id, "name": item.name, "count": item.count, "enabled": item.enabled, "note": item.note})
@app.delete("/widgets/<int:id>")
def delete_widget(id):
    with Session(engine) as session:
        item = session.get(Widget, id)
        if item is None:
            return jsonify({"error": "not found"}), 404
        session.delete(item)
        session.commit()
        return "", 204
"""
    create = indent(
        widget_validation("data", False, 'return jsonify({"error": "invalid request body"}), 400'),
        "    ",
    )
    patch = indent(
        widget_validation("data", True, 'return jsonify({"error": "invalid request body"}), 400'),
        "    ",
    )
    replace = indent(
        widget_validation("data", False, 'return jsonify({"error": "invalid request body"}), 400'),
        "    ",
    )
    text = text.replace(
        "def create_widget():\n    data = request.get_json()\n    with Session(engine) as session:\n",
        "def create_widget():\n    data = request.get_json()\n"
        + create
        + "    with Session(engine) as session:\n",
    )
    text = text.replace(
        "def patch_widget(id):\n    data = request.get_json()\n    with Session(engine) as session:\n",
        "def patch_widget(id):\n    data = request.get_json()\n"
        + patch
        + "    with Session(engine) as session:\n",
    )
    return text.replace(
        "def replace_widget(id):\n    data = request.get_json()\n    with Session(engine) as session:\n",
        "def replace_widget(id):\n    data = request.get_json()\n"
        + replace
        + "    with Session(engine) as session:\n",
    )


def fastapi_write_source() -> str:
    text = """from fastapi import FastAPI, HTTPException
from models import Widget
from os import environ
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
engine = create_engine(environ["DATABASE_URL"])
app = FastAPI()
@app.post("/widgets", status_code=201)
def create_widget(data: dict):
    with Session(engine) as session:
        item = Widget(name=data["name"], count=data["count"], enabled=data["enabled"], note=data.get("note"))
        session.add(item)
        session.commit()
        session.refresh(item)
        return {"id": item.id, "name": item.name, "count": item.count, "enabled": item.enabled, "note": item.note}
@app.patch("/widgets/{id}")
def patch_widget(id: int, data: dict):
    with Session(engine) as session:
        item = session.get(Widget, id)
        if item is None:
            raise HTTPException(status_code=404, detail="not found")
        if "name" in data:
            item.name = data["name"]
        if "count" in data:
            item.count = data["count"]
        if "enabled" in data:
            item.enabled = data["enabled"]
        if "note" in data:
            item.note = data["note"]
        session.commit()
        session.refresh(item)
        return {"id": item.id, "name": item.name, "count": item.count, "enabled": item.enabled, "note": item.note}
@app.put("/widgets/{id}")
def replace_widget(id: int, data: dict):
    with Session(engine) as session:
        item = session.get(Widget, id)
        if item is None:
            raise HTTPException(status_code=404, detail="not found")
        item.name = data["name"]
        item.count = data["count"]
        item.enabled = data["enabled"]
        item.note = data.get("note")
        session.commit()
        session.refresh(item)
        return {"id": item.id, "name": item.name, "count": item.count, "enabled": item.enabled, "note": item.note}
@app.delete("/widgets/{id}", status_code=204)
def delete_widget(id: int):
    with Session(engine) as session:
        item = session.get(Widget, id)
        if item is None:
            raise HTTPException(status_code=404, detail="not found")
        session.delete(item)
        session.commit()
"""
    create = indent(
        widget_validation(
            "data", False, 'raise HTTPException(status_code=400, detail="invalid request body")'
        ),
        "    ",
    )
    patch = indent(
        widget_validation(
            "data", True, 'raise HTTPException(status_code=400, detail="invalid request body")'
        ),
        "    ",
    )
    replace = indent(
        widget_validation(
            "data", False, 'raise HTTPException(status_code=400, detail="invalid request body")'
        ),
        "    ",
    )
    text = text.replace(
        "def create_widget(data: dict):\n    with Session(engine) as session:\n",
        "def create_widget(data: dict):\n" + create + "    with Session(engine) as session:\n",
    )
    text = text.replace(
        "def patch_widget(id: int, data: dict):\n    with Session(engine) as session:\n",
        "def patch_widget(id: int, data: dict):\n"
        + patch
        + "    with Session(engine) as session:\n",
    )
    return text.replace(
        "def replace_widget(id: int, data: dict):\n    with Session(engine) as session:\n",
        "def replace_widget(id: int, data: dict):\n"
        + replace
        + "    with Session(engine) as session:\n",
    )


def drf_write_source() -> str:
    text = """from django.urls import path
from rest_framework.decorators import api_view, authentication_classes, permission_classes, renderer_classes
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from models import Widget
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@renderer_classes([JSONRenderer])
def create_widget(request):
    item = Widget.objects.create(name=request.data["name"], count=request.data["count"], enabled=request.data["enabled"], note=request.data.get("note"))
    return Response({"id": item.id, "name": item.name, "count": item.count, "enabled": item.enabled, "note": item.note}, status=201)
@api_view(["PATCH"])
@authentication_classes([])
@permission_classes([AllowAny])
@renderer_classes([JSONRenderer])
def patch_widget(request, id):
    item = Widget.objects.filter(id=id).first()
    if item is None:
        return Response({"error": "not found"}, status=404)
    if "name" in request.data:
        item.name = request.data["name"]
    if "count" in request.data:
        item.count = request.data["count"]
    if "enabled" in request.data:
        item.enabled = request.data["enabled"]
    if "note" in request.data:
        item.note = request.data["note"]
    item.save(update_fields=list(request.data))
    return Response({"id": item.id, "name": item.name, "count": item.count, "enabled": item.enabled, "note": item.note})
@api_view(["PUT"])
@authentication_classes([])
@permission_classes([AllowAny])
@renderer_classes([JSONRenderer])
def replace_widget(request, id):
    item = Widget.objects.filter(id=id).first()
    if item is None:
        return Response({"error": "not found"}, status=404)
    item.name = request.data["name"]
    item.count = request.data["count"]
    item.enabled = request.data["enabled"]
    item.note = request.data.get("note")
    item.save(update_fields=["name", "count", "enabled", "note"])
    return Response({"id": item.id, "name": item.name, "count": item.count, "enabled": item.enabled, "note": item.note})
@api_view(["DELETE"])
@authentication_classes([])
@permission_classes([AllowAny])
@renderer_classes([JSONRenderer])
def delete_widget(request, id):
    item = Widget.objects.filter(id=id).first()
    if item is None:
        return Response({"error": "not found"}, status=404)
    item.delete()
    return Response(status=204)
urlpatterns = [path("widgets", create_widget), path("widgets/<int:id>", patch_widget), path("widgets/<int:id>", replace_widget), path("widgets/<int:id>", delete_widget)]
"""
    create = indent(
        widget_validation(
            "request.data",
            False,
            'return Response({"error": "invalid request body"}, status=400)',
        ),
        "    ",
    )
    patch = indent(
        widget_validation(
            "request.data",
            True,
            'return Response({"error": "invalid request body"}, status=400)',
        ),
        "    ",
    )
    replace = indent(
        widget_validation(
            "request.data",
            False,
            'return Response({"error": "invalid request body"}, status=400)',
        ),
        "    ",
    )
    text = text.replace(
        "    item = Widget.objects.create", create + "    item = Widget.objects.create", 1
    )
    text = text.replace(
        "    item = Widget.objects.filter", patch + "    item = Widget.objects.filter", 1
    )
    marker = "def replace_widget(request, id):\n    item = Widget.objects.filter"
    return text.replace(
        marker, "def replace_widget(request, id):\n" + replace + "    item = Widget.objects.filter"
    )


def write_contract() -> dict[str, object]:
    fields = [
        {
            "name": "id",
            "sql_type": "integer",
            "go_type": "int32",
            "nullable": False,
            "primary_key": True,
            "unique": False,
            "auto": True,
        },
        {
            "name": "name",
            "sql_type": "varchar(80)",
            "go_type": "string",
            "nullable": False,
            "primary_key": False,
            "unique": True,
            "auto": False,
        },
        {
            "name": "count",
            "sql_type": "integer",
            "go_type": "int32",
            "nullable": False,
            "primary_key": False,
            "unique": False,
            "auto": False,
        },
        {
            "name": "enabled",
            "sql_type": "boolean",
            "go_type": "bool",
            "nullable": False,
            "primary_key": False,
            "unique": False,
            "auto": False,
        },
        {
            "name": "note",
            "sql_type": "text",
            "go_type": "string",
            "nullable": True,
            "primary_key": False,
            "unique": False,
            "auto": False,
        },
    ]
    return {
        "schema": "sanka.python-to-golang.capture/v1",
        "source_digest": "sha256:test",
        "configuration": {
            "source_framework": "flask",
            "target_framework": "fiber",
            "source_file": "app.py",
            "database_layer": "pgx",
            "database_dialect": "postgresql",
            "migration_tool": "goose",
            "schema_mode": "empty",
            "models_file": "models.py",
        },
        "models": [{"name": "Widget", "table": "widgets", "fields": fields}],
        "routes": [
            {
                "path": "/widgets",
                "method": "POST",
                "status": 201,
                "write": {"operation": "create", "model": "Widget"},
            },
            {
                "path": "/widgets/:id",
                "method": "PATCH",
                "status": 200,
                "write": {"operation": "patch", "model": "Widget", "lookup": "id"},
            },
            {
                "path": "/widgets/:id",
                "method": "PUT",
                "status": 200,
                "write": {"operation": "replace", "model": "Widget", "lookup": "id"},
            },
            {
                "path": "/widgets/:id",
                "method": "DELETE",
                "status": 204,
                "write": {"operation": "delete", "model": "Widget", "lookup": "id"},
            },
        ],
        "gaps": [],
        "scope": "bounded PostgreSQL writes",
        "complete_backend": False,
    }


@pytest.mark.parametrize("target", TARGETS)
def test_write_generation_preserves_presence_and_transactions(tmp_path: Path, target: str) -> None:
    captured = write_contract()
    captured["configuration"]["target_framework"] = target  # type: ignore[index]
    files = render(captured)
    source = files["app.go"]
    assert '"/widgets"' in source
    expected_path = '"/widgets/:id"' if target in {"fiber", "gin"} else '"/widgets/{id}"'
    assert expected_path in source
    assert "map[string]json.RawMessage" in source
    assert "pgx.BeginFunc" in source
    assert 'INSERT INTO \\"widgets\\"' in source
    assert 'UPDATE \\"widgets\\"' in source
    assert 'DELETE FROM \\"widgets\\"' in source
    assert "$" in source
    for name, content in files.items():
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)
    if os.getenv("SANKA_GO_TESTS") == "1":
        result = subprocess.run(
            ["go", "test", "-mod=readonly", "-p=2", "./..."],
            cwd=tmp_path,
            env=os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"},
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr


def test_public_replay_fails_closed_until_shared_adapter_is_adopted(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="write replay requires the versioned shared HTTP"):
        replay(tmp_path, tmp_path / "missing", write_contract(), "test")


@pytest.mark.parametrize(
    ("framework", "source"),
    [("flask", flask_write_source), ("fastapi", fastapi_write_source), ("drf", drf_write_source)],
)
def test_write_recipe_is_captured_without_execution(
    tmp_path: Path, framework: str, source: object
) -> None:
    (tmp_path / "app.py").write_text(source())  # type: ignore[operator]
    (tmp_path / "models.py").write_text(model_source(framework))
    captured = capture(
        tmp_path,
        configuration({"source_framework": framework, "database_layer": "pgx"}),
    )
    assert captured["gaps"] == []
    assert captured["routes"] == [
        {
            "path": "/widgets",
            "method": "POST",
            "status": 201,
            "write": {"operation": "create", "model": "Widget"},
        },
        {
            "path": "/widgets/:id",
            "method": "PATCH",
            "status": 200,
            "write": {"operation": "patch", "model": "Widget", "lookup": "id"},
        },
        {
            "path": "/widgets/:id",
            "method": "PUT",
            "status": 200,
            "write": {"operation": "replace", "model": "Widget", "lookup": "id"},
        },
        {
            "path": "/widgets/:id",
            "method": "DELETE",
            "status": 204,
            "write": {"operation": "delete", "model": "Widget", "lookup": "id"},
        },
    ]
    files = render(captured)
    assert files == render(
        capture(
            tmp_path,
            configuration({"source_framework": framework, "database_layer": "pgx"}),
        )
    )
    if os.getenv("SANKA_GO_TESTS") == "1":
        candidate = tmp_path / "candidate"
        for name, content in files.items():
            destination = candidate / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content)
        result = subprocess.run(
            ["go", "test", "-mod=readonly", "-p=2", "./..."],
            cwd=candidate,
            env=os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"},
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("framework", "source"),
    [("flask", flask_write_source), ("fastapi", fastapi_write_source), ("drf", drf_write_source)],
)
def test_changed_write_semantics_fail_closed(
    tmp_path: Path, framework: str, source: object
) -> None:
    changed = source().replace("<= 2147483647", "<= 2147483648")  # type: ignore[operator]
    (tmp_path / "app.py").write_text(changed)
    (tmp_path / "models.py").write_text(model_source(framework))
    captured = capture(
        tmp_path,
        configuration({"source_framework": framework, "database_layer": "pgx"}),
    )
    assert captured["gaps"]
    assert captured["routes"] == []


def test_fastapi_delete_requires_explicit_204(tmp_path: Path) -> None:
    changed = fastapi_write_source().replace(
        '@app.delete("/widgets/{id}", status_code=204)', '@app.delete("/widgets/{id}")'
    )
    (tmp_path / "app.py").write_text(changed)
    (tmp_path / "models.py").write_text(model_source("fastapi"))
    captured = capture(
        tmp_path,
        configuration({"source_framework": "fastapi", "database_layer": "pgx"}),
    )
    assert captured["gaps"]
    assert captured["routes"] == []


@pytest.mark.parametrize(
    ("framework", "source", "before", "after"),
    [
        ("flask", flask_write_source, 'item.note = data.get("note")', 'item.note = data["note"]'),
        (
            "fastapi",
            fastapi_write_source,
            'item.note = data.get("note")',
            'item.note = data["note"]',
        ),
        (
            "drf",
            drf_write_source,
            'item.note = request.data.get("note")',
            'item.note = request.data["note"]',
        ),
    ],
)
def test_changed_put_replacement_fails_closed(
    tmp_path: Path, framework: str, source: object, before: str, after: str
) -> None:
    (tmp_path / "app.py").write_text(source().replace(before, after))  # type: ignore[operator]
    (tmp_path / "models.py").write_text(model_source(framework))
    captured = capture(
        tmp_path,
        configuration({"source_framework": framework, "database_layer": "pgx"}),
    )
    assert captured["gaps"]
    assert not any(
        route.get("write", {}).get("operation") == "replace" for route in captured["routes"]
    )


GO_WRITE_TEST = r"""package backend
import (
    "context"
    "encoding/json"
    "github.com/jackc/pgx/v5/pgxpool"
    EXTRA_IMPORT
    "net/http/httptest"
    "os"
    "strings"
    "testing"
    "time"
)
func TestWriteLifecycle(t *testing.T) {
    ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
    defer cancel()
    pool, err := pgxpool.New(ctx, os.Getenv("DATABASE_URL"))
    if err != nil { t.Fatal(err) }
    defer pool.Close()
    app := NewApp(pool)
    cases := []struct { method, path, body string; status int }{
        {"POST", "/widgets", `{"name":"bad"}`, 400},
        {"POST", "/widgets", `{"name":"bad","count":null,"enabled":false}`, 400},
        {"POST", "/widgets", `{"name":"bad","count":1,"enabled":false,"extra":1}`, 400},
        {"POST", "/widgets", `{"name":"alpha","count":7,"enabled":true,"note":null}`, 201},
        {"PATCH", "/widgets/1", `{"count":0,"enabled":false,"note":""}`, 200},
        {"PATCH", "/widgets/1", `{}`, 200},
        {"PATCH", "/widgets/1", `{"note":null}`, 200},
        {"PATCH", "/widgets/nope", `{"count":1}`, 400},
        {"PATCH", "/widgets/999", `{"count":1}`, 404},
        {"PATCH", "/widgets/1", `{"count":1} trailing`, 400},
        {"PUT", "/widgets/1", `{"name":"missing"}`, 400},
        {"PUT", "/widgets/nope", `{"name":"beta","count":9,"enabled":false}`, 400},
        {"PUT", "/widgets/999", `{"name":"beta","count":9,"enabled":false}`, 404},
        {"PUT", "/widgets/1", `{"name":"beta","count":9,"enabled":false}`, 200},
        {"DELETE", "/widgets/999", ``, 404},
        {"DELETE", "/widgets/1", ``, 204},
        {"DELETE", "/widgets/1", ``, 404},
    }
    for _, item := range cases {
        request := httptest.NewRequest(item.method, item.path, strings.NewReader(item.body))
        request.Header.Set("Content-Type", "application/json")
        EXCHANGE
        if status != item.status || (status != 204 && !json.Valid(body)) || (status == 204 && len(body) != 0) {
            t.Fatalf("%s %s: status=%d body=%s", item.method, item.path, status, body)
        }
    }
}
"""


def go_write_test(target: str) -> str:
    if target == "fiber":
        exchange = """response, err := app.Test(request)
        if err != nil { t.Fatal(err) }
        body, err := io.ReadAll(response.Body)
        response.Body.Close()
        if err != nil { t.Fatal(err) }
        status := response.StatusCode"""
        extra_import = '"io"'
    else:
        exchange = """response := httptest.NewRecorder()
        app.ServeHTTP(response, request)
        body := response.Body.Bytes()
        status := response.Code"""
        extra_import = ""
    return GO_WRITE_TEST.replace("EXTRA_IMPORT", extra_import).replace("EXCHANGE", exchange)


@pytest.mark.skipif(
    os.getenv("SANKA_GO_TESTS") != "1" or not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN"),
    reason="requires qualified Go toolchain and explicit PostgreSQL fixture",
)
@pytest.mark.parametrize(
    ("framework", "source"),
    [("flask", flask_write_source), ("fastapi", fastapi_write_source), ("drf", drf_write_source)],
)
@pytest.mark.parametrize("target", TARGETS)
def test_write_lifecycle_and_database_effects(
    tmp_path: Path, framework: str, source: object, target: str
) -> None:
    import psycopg
    from psycopg import sql

    output = generate(tmp_path, framework, target, app_source=source())  # type: ignore[operator]
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    schema = "go_write_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            url = schema_dsn(dsn, schema)
            environment = os.environ | {
                "DATABASE_URL": url,
                "GOTOOLCHAIN": "local",
                "GOWORK": "off",
                "GOMAXPROCS": "2",
            }
            subprocess.run(
                ["go", "run", "-mod=readonly", "./cmd/migrate", "up"],
                cwd=output,
                env=environment,
                check=True,
                capture_output=True,
                timeout=180,
            )
            (output / "write_contract_test.go").write_text(go_write_test(target))
            result = subprocess.run(
                [
                    "go",
                    "test",
                    "-mod=readonly",
                    "-count=1",
                    "-p=2",
                    "-run",
                    "TestWriteLifecycle",
                    ".",
                ],
                cwd=output,
                env=environment,
                capture_output=True,
                text=True,
                timeout=180,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            with psycopg.connect(url) as connection:
                assert (
                    connection.execute(
                        'SELECT id, name, count, enabled, note FROM "widgets"'
                    ).fetchall()
                    == []
                )
        finally:
            admin.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )

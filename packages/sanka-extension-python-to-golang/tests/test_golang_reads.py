# SPDX-License-Identifier: Apache-2.0
"""Real database-backed HTTP responses through the public extension lifecycle."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sanka_extension_python_to_golang.adapter import handle
from sanka_extension_python_to_golang.capture import SOURCES, TARGETS, capture, configuration
from sanka_extension_python_to_golang.replay import request_paths
from test_golang_schema import SOURCE_DDL, generate, model_source, schema_dsn
from test_python_to_golang import PAYLOAD, request, source


def detail_source(framework: str) -> str:
    fields = (
        '"id": item.id, "name": item.name, "count": item.count, '
        '"enabled": item.enabled, "note": item.note'
    )
    if framework == "drf":
        return f"""from django.urls import path
from rest_framework.decorators import (
    api_view, authentication_classes, permission_classes, renderer_classes,
)
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from models import Widget
@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
@renderer_classes([JSONRenderer])
def get_widget(request, id):
    item = Widget.objects.filter(id=id).first()
    if item is None:
        return Response({{"error": "not found"}}, status=404)
    return Response({{{fields}}})
urlpatterns = [path("widgets/<int:id>", get_widget)]
"""
    prefix = """from models import Widget
from os import environ
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
engine = create_engine(environ["DATABASE_URL"])
"""
    if framework == "flask":
        return (
            prefix
            + f"""from flask import Flask, jsonify
app = Flask(__name__)
@app.get("/widgets/<int:id>")
def get_widget(id):
    with Session(engine) as session:
        item = session.get(Widget, id)
        if item is None:
            return jsonify({{"error": "not found"}}), 404
        return jsonify({{{fields}}})
"""
        )
    return (
        prefix
        + f"""from fastapi import FastAPI, HTTPException
app = FastAPI()
@app.get("/widgets/{{id}}")
def get_widget(id: int):
    with Session(engine) as session:
        item = session.get(Widget, id)
        if item is None:
            raise HTTPException(status_code=404, detail="not found")
        return {{{fields}}}
"""
    )


def read_source(framework: str) -> str:
    text = source(framework).replace("async def", "def")
    if framework == "drf":
        text = "from models import Widget\n" + text
        return text.replace(
            f"Response({PAYLOAD!r})",
            'Response(list(Widget.objects.order_by("id").values('
            '"id", "name", "count", "enabled", "note")[:2]))',
        )
    prefix = (
        "from models import Widget\nfrom os import environ\n"
        "from sqlalchemy import create_engine, select\nfrom sqlalchemy.orm import Session\n"
        'engine = create_engine(environ["DATABASE_URL"])\n'
    )
    value = (
        "[dict(row) for row in session.execute(select(Widget.id, Widget.name, Widget.count, "
        "Widget.enabled, Widget.note).order_by(Widget.id).limit(2)).mappings()]"
    )
    old = repr(PAYLOAD)
    if framework == "flask":
        value, old = f"jsonify({value})", f"jsonify({old})"
    return prefix + text.replace(
        "return " + old, "with Session(engine) as session:\n        return " + value
    )


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
def test_read_generation(tmp_path: Path, framework: str, target: str) -> None:
    output = generate(tmp_path, framework, target, app_source=read_source(framework))
    plan = json.loads((tmp_path / ".sanka/go/plan.json").read_text())
    assert plan["capture"]["routes"][0]["read"] == {"model": "Widget", "order_by": "id", "limit": 2}
    assert "LIMIT $1" in (output / "app.go").read_text()
    if os.getenv("SANKA_GO_TESTS") == "1":
        subprocess.run(
            ["go", "test", "-mod=readonly", "-p=2", "./..."],
            cwd=output,
            env=os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"},
            check=True,
            capture_output=True,
            timeout=180,
        )


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
def test_detail_read_generation(tmp_path: Path, framework: str, target: str) -> None:
    output = generate(tmp_path, framework, target, app_source=detail_source(framework))
    plan = json.loads((tmp_path / ".sanka/go/plan.json").read_text())
    assert plan["capture"]["routes"][0]["read"] == {"model": "Widget", "lookup": "id"}
    source_text = (output / "app.go").read_text()
    assert 'WHERE \\"id\\" = $1' in source_text
    if os.getenv("SANKA_GO_TESTS") == "1":
        subprocess.run(
            ["go", "test", "-mod=readonly", "-p=2", "./..."],
            cwd=output,
            env=os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"},
            check=True,
            capture_output=True,
            timeout=180,
        )


@pytest.mark.parametrize("framework", SOURCES)
def test_changed_detail_read_semantics_block(tmp_path: Path, framework: str) -> None:
    text = detail_source(framework).replace('"not found"', '"missing"')
    (tmp_path / "app.py").write_text(text)
    (tmp_path / "models.py").write_text(model_source(framework))
    result = handle(
        dataclasses.replace(
            request(tmp_path, framework),
            configuration={"source_framework": framework, "database_layer": "pgx"},
        )
    )
    assert result.data["capture"]["gaps"]
    assert result.data["files"] == {}


def test_detail_read_replay_uses_a_concrete_lookup(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(detail_source("flask"))
    (tmp_path / "models.py").write_text(model_source("flask"))
    captured = capture(
        tmp_path, configuration({"source_framework": "flask", "database_layer": "pgx"})
    )
    assert request_paths(captured["routes"][0]) == ["/widgets/1"]


def test_detail_read_requires_matching_route_parameter(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        detail_source("flask").replace('"/widgets/<int:id>"', '"/widgets"')
    )
    (tmp_path / "models.py").write_text(model_source("flask"))
    captured = capture(
        tmp_path, configuration({"source_framework": "flask", "database_layer": "pgx"})
    )
    assert "detail read lookup must match" in " ".join(captured["gaps"])


@pytest.mark.skipif(
    not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN") or os.getenv("SANKA_GO_TESTS") != "1",
    reason="requires explicit test PostgreSQL DSN and SANKA_GO_TESTS=1",
)
@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("grouped", [False, True])
def test_detail_read_database_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, framework: str, target: str, grouped: bool
) -> None:
    import psycopg
    from psycopg import sql
    from test_golang_routing import group_backend

    text = detail_source(framework)
    if grouped:
        (tmp_path / "routes.py").write_text(group_backend(text, framework))
        text = (
            "from routes import urlpatterns\n"
            if framework == "drf"
            else "from routes import app, engine\n"
        )
    output = generate(tmp_path, framework, target, app_source=text)
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    schemas = ["go_detail_" + uuid.uuid4().hex for _ in range(2)]
    environment = os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"}
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            for schema in schemas:
                admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            source_url, target_url = [schema_dsn(dsn, schema) for schema in schemas]
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
            row = (1, "detail", 7, True, None)
            for url in (source_url, target_url):
                with psycopg.connect(url, autocommit=True) as connection:
                    connection.execute(
                        "INSERT INTO widgets (id,name,count,enabled,note) VALUES (%s,%s,%s,%s,%s)",
                        row,
                    )
            monkeypatch.setenv(
                "SANKA_GO_SOURCE_TEST_DATABASE_URL",
                source_url
                if framework == "drf"
                else source_url.replace("postgresql://", "postgresql+psycopg://", 1),
            )
            monkeypatch.setenv("SANKA_GO_TARGET_TEST_DATABASE_URL", target_url)
            req = dataclasses.replace(
                request(tmp_path, framework, target),
                command="verify",
                configuration={
                    "source_framework": framework,
                    "target_framework": target,
                    "database_layer": "pgx",
                },
            )
            verified = handle(req)
            assert verified.outcome == "success", verified.error
            expected = dict(zip(("id", "name", "count", "enabled", "note"), row, strict=True))
            assert verified.data["source"][0]["body"] == expected
            assert verified.data["candidate"][0]["body"] == expected
            with psycopg.connect(target_url, autocommit=True) as connection:
                connection.execute("DELETE FROM widgets WHERE id=1")
            mismatch = handle(req)
            assert mismatch.outcome == "error"
            assert mismatch.error.code == "SANKA_EXTENSION_PARITY_FAILED"
            report = json.loads((tmp_path / ".sanka/go/verify.json").read_text())
            assert report["candidate"][0] == {
                "path": "/api/widgets/1" if grouped else "/widgets/1",
                "status": 404,
                "media_type": "application/json",
                "body": {"detail" if framework == "fastapi" else "error": "not found"},
            }
        finally:
            for schema in schemas:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize(
    "change", ["unordered", "unbounded", "projection", "side_effect", "async", "zero", "large"]
)
def test_unknown_queries_block(tmp_path: Path, framework: str, change: str) -> None:
    text = read_source(framework)
    replacements = {
        "unordered": ('.order_by("id")', "")
        if framework == "drf"
        else (".order_by(Widget.id)", ""),
        "unbounded": ("[:2]", "") if framework == "drf" else (".limit(2)", ""),
        "projection": ('"note"', '"missing"')
        if framework == "drf"
        else ("Widget.note", "Widget.missing"),
        "side_effect": (
            "def health(request):",
            "def health(request):\n    Widget.objects.all().delete()",
        )
        if framework == "drf"
        else ("def health():", "def health():\n    engine.dispose()"),
        "async": ("def health", "async def health"),
        "zero": ("[:2]", "[:0]") if framework == "drf" else (".limit(2)", ".limit(0)"),
        "large": ("[:2]", "[:1001]") if framework == "drf" else (".limit(2)", ".limit(1001)"),
    }
    before, after = replacements[change]
    (tmp_path / "app.py").write_text(text.replace(before, after))
    (tmp_path / "models.py").write_text(model_source(framework))
    req = dataclasses.replace(
        request(tmp_path, framework),
        configuration={"source_framework": framework, "database_layer": "pgx"},
    )
    planned = handle(req)
    assert planned.outcome == "success", planned.error
    assert planned.data["capture"]["gaps"]
    assert planned.data["files"] == {}


def test_read_replay_requires_explicit_fixture_databases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generate(tmp_path, "flask", "fiber", app_source=read_source("flask"))
    for key in ("SANKA_GO_SOURCE_TEST_DATABASE_URL", "SANKA_GO_TARGET_TEST_DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://must-not-use/production")
    req = dataclasses.replace(
        request(tmp_path),
        command="verify",
        configuration={"source_framework": "flask", "database_layer": "pgx"},
    )
    result = handle(req)
    assert result.outcome == "error"
    assert "requires explicit" in result.error.message
    assert not (tmp_path / ".sanka/go/verify.json").exists()


@pytest.mark.skipif(
    not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN") or os.getenv("SANKA_GO_TESTS") != "1",
    reason="requires explicit test PostgreSQL DSN and SANKA_GO_TESTS=1",
)
@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
def test_database_read_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, framework: str, target: str
) -> None:
    import psycopg
    from psycopg import sql

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_database.py").write_text(
        "import os\n"
        "from sqlalchemy import create_engine, text\n"
        "def test_disposable_database():\n"
        "    url = os.environ['DATABASE_URL']\n"
        "    assert '/sanka_verify_' in url and 'options=' not in url\n"
        "    with create_engine(url).begin() as db:\n"
        "        assert db.execute(text(\"SELECT to_regclass('widgets')\")).scalar() is None\n"
        if framework == "fastapi" and target == "fiber"
        else "raise RuntimeError('original tests must not run')"
    )
    output = generate(tmp_path, framework, target, app_source=read_source(framework))
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    schemas = ["go_read_" + uuid.uuid4().hex for _ in range(2)]
    environment = os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"}
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            for schema in schemas:
                admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            source_url, target_url = [schema_dsn(dsn, schema) for schema in schemas]
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
            monkeypatch.setenv(
                "SANKA_GO_SOURCE_TEST_DATABASE_URL",
                source_url
                if framework == "drf"
                else source_url.replace("postgresql://", "postgresql+psycopg://", 1),
            )
            monkeypatch.setenv("SANKA_GO_TARGET_TEST_DATABASE_URL", target_url)
            req = dataclasses.replace(
                request(tmp_path, framework, target),
                command="verify",
                configuration={
                    "source_framework": framework,
                    "target_framework": target,
                    "database_layer": "pgx",
                },
            )
            empty = handle(req)
            assert empty.outcome == "success", empty.error
            assert empty.data["source"][0]["body"] == []
            rows = [
                (9223372036854775807, "last", 1, True, "omitted by limit"),
                (9007199254740993, "日本語", -2147483648, False, None),
                (9007199254740994, "<tag>&", 2147483647, True, "line\nbreak"),
            ]
            for url in (source_url, target_url):
                with psycopg.connect(url, autocommit=True) as connection:
                    for row in rows:
                        connection.execute(
                            "INSERT INTO widgets (id,name,count,enabled,note) "
                            "VALUES (%s,%s,%s,%s,%s)",
                            row,
                        )
            populated = handle(req)
            assert populated.outcome == "success", populated.error
            expected = [
                dict(zip(("id", "name", "count", "enabled", "note"), row, strict=True))
                for row in rows[1:]
            ]
            assert populated.data["candidate"][0]["body"] == expected
            assert populated.data["source"][0]["body"] == expected
            if framework == "fastapi" and target == "fiber":
                monkeypatch.setenv("SANKA_GO_RUN_ORIGINAL_TESTS", "1")
                qualified = handle(req)
                assert qualified.outcome == "success", qualified.error
                assert qualified.data["original_tests"]["tests_run"] == 1
                assert qualified.data["qualification"]["original_tests_executed"]
                monkeypatch.delenv("SANKA_GO_RUN_ORIGINAL_TESTS")
            assert handle(dataclasses.replace(req, command="test")).outcome == "success"
            # A changed fixture must cause public verify to fail, not reuse passing evidence.
            with psycopg.connect(target_url, autocommit=True) as connection:
                connection.execute("UPDATE widgets SET enabled=true WHERE id=9007199254740993")
            mismatch = handle(req)
            assert mismatch.outcome == "error"
            assert mismatch.error.code == "SANKA_EXTENSION_PARITY_FAILED"
            # Database errors produce a bounded generic response without leaking SQL/credentials.
            with psycopg.connect(target_url, autocommit=True) as connection:
                connection.execute("DROP TABLE widgets")
            failed = handle(dataclasses.replace(req, command="test"))
            assert failed.outcome == "error"
            report = json.loads((tmp_path / ".sanka/go/test.json").read_text())
            assert report["ok"] is False
            assert report["candidate"][0] == {
                "path": "/health",
                "status": 500,
                "media_type": "application/json",
                "body": {"error": "database read failed"},
            }
        finally:
            for schema in schemas:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )


@pytest.mark.parametrize("url", ["sqlite:///fixture", "postgresql:///implicit-host", "not-a-url"])
def test_read_replay_rejects_ambiguous_database_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    generate(tmp_path, "flask", "fiber", app_source=read_source("flask"))
    monkeypatch.setenv("SANKA_GO_TARGET_TEST_DATABASE_URL", url)
    result = handle(
        dataclasses.replace(
            request(tmp_path),
            command="test",
            configuration={"source_framework": "flask", "database_layer": "pgx"},
        )
    )
    assert result.outcome == "error"
    assert "fixture database URL" in result.error.message


def test_read_plan_is_location_independent(tmp_path: Path) -> None:
    plans = []
    for name in ("first", "second"):
        root = tmp_path / name
        root.mkdir()
        generate(root, "fastapi", "fiber", app_source=read_source("fastapi"))
        plans.append(json.loads((root / ".sanka/go/plan.json").read_text()))
    assert plans[0] == plans[1]


@pytest.mark.parametrize("framework", SOURCES)
def test_model_import_cannot_replace_framework_symbols(tmp_path: Path, framework: str) -> None:
    text = read_source(framework)
    models = model_source(framework)
    if framework == "drf":
        text = text.replace(
            "from rest_framework.response import Response", "from models import Response"
        )
        models += """
class Response(models.Model):
    id = models.BigAutoField(primary_key=True)
    class Meta:
        app_label = "catalog"
        db_table = "responses"
"""
    else:
        text = text.replace("from sqlalchemy.orm import Session", "from models import Session")
        models += """
class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
"""
    (tmp_path / "app.py").write_text(text)
    (tmp_path / "models.py").write_text(models)
    plan = handle(
        dataclasses.replace(
            request(tmp_path, framework),
            configuration={"source_framework": framework, "database_layer": "pgx"},
        )
    )
    assert plan.data["files"] == {}
    assert any("unsupported or duplicate import" in gap for gap in plan.data["capture"]["gaps"])

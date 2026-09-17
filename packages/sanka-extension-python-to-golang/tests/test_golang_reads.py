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
from sanka_extension_python_to_golang.capture import SOURCES, TARGETS
from test_golang_schema import SOURCE_DDL, generate, model_source, schema_dsn
from test_python_to_golang import PAYLOAD, request, source


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

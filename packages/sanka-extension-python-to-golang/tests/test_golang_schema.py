# SPDX-License-Identifier: Apache-2.0
"""Model metadata and real PostgreSQL schema equivalence through generated Goose."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest
from sanka_extension_python_to_golang.adapter import handle
from sanka_extension_python_to_golang.capture import SOURCES, TARGETS
from sanka_extension_python_to_golang.models import capture_models
from test_python_to_golang import request, source


def model_source(framework: str) -> str:
    if framework == "drf":
        return """from django.db import models
class Widget(models.Model):
    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=40, unique=True)
    count = models.IntegerField()
    enabled = models.BooleanField()
    note = models.TextField(null=True)
    class Meta:
        app_label = "catalog"
        db_table = "widgets"
"""
    return """from sqlalchemy import BigInteger, Integer, Boolean, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
class Base(DeclarativeBase):
    pass
class Widget(Base):
    __tablename__ = "widgets"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(40), unique=True)
    count: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean)
    note: Mapped[str | None] = mapped_column(Text)
"""


def generate(root: Path, framework: str, target: str) -> Path:
    (root / "app.py").write_text(source(framework))
    (root / "models.py").write_text(model_source(framework))
    req = request(root, framework, target)
    req = dataclasses.replace(req, configuration=req.configuration | {"database_layer": "pgx"})
    planned = handle(req)
    assert planned.outcome == "success", planned.error
    assert not planned.data["capture"]["gaps"], planned.data
    assert planned.data == handle(req).data
    result = handle(
        dataclasses.replace(
            req,
            command="apply",
            reviewed_plan_hash="runtime-review",
            configuration=req.configuration | {"extension_plan_hash": planned.data["plan_hash"]},
        )
    )
    assert result.outcome == "success", result.error
    return Path(result.data["output"])


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
def test_schema_generation(tmp_path: Path, framework: str, target: str) -> None:
    output = generate(tmp_path, framework, target)
    assert "Note *string" in (output / "models.go").read_text()
    assert "Count int32" in (output / "models.go").read_text()
    assert "Id int64" in (output / "models.go").read_text()
    migration = (output / "migrations/00001_initial.sql").read_text()
    assert '"name" varchar(40) NOT NULL UNIQUE' in migration
    assert "existing tables are never adopted" in migration
    assert "NewPostgresSessionLocker" in (output / "database.go").read_text()
    assert (output / "cmd/migrate/main.go").is_file()
    assert not (output / "services").exists()
    if os.getenv("SANKA_GO_TESTS") == "1":
        result = subprocess.run(
            ["go", "test", "-mod=readonly", "-p=2", "./..."],
            cwd=output,
            env=os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"},
            text=True,
            capture_output=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize(
    "option", ["default=1", "db_index=True", "server_default='1'", "primary_key=1"]
)
def test_unsupported_model_options_block(tmp_path: Path, framework: str, option: str) -> None:
    text = model_source(framework).replace("unique=True", option)
    path = tmp_path / "models.py"
    path.write_text(text)
    with pytest.raises(ValueError):
        capture_models(path, framework)


def test_unknown_models_block_apply(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(source("flask"))
    (tmp_path / "models.py").write_text("raise RuntimeError('never execute')")
    req = request(tmp_path)
    req = dataclasses.replace(req, configuration=req.configuration | {"database_layer": "pgx"})
    planned = handle(req)
    assert planned.data["files"] == {}
    assert planned.data["capture"]["gaps"]
    assert (
        handle(
            dataclasses.replace(
                req,
                command="apply",
                reviewed_plan_hash="runtime-review",
                configuration=req.configuration
                | {"extension_plan_hash": planned.data["plan_hash"]},
            )
        ).outcome
        == "error"
    )


SOURCE_DDL = """
import importlib.util, sys
from urllib.parse import urlsplit, parse_qs, unquote
framework, filename, dsn = sys.argv[1:]
if framework == "drf":
    from django.conf import settings
    url = urlsplit(dsn)
    settings.configure(SECRET_KEY="fixture", INSTALLED_APPS=[], DATABASES={"default": {
        "ENGINE": "django.db.backends.postgresql", "NAME": url.path.lstrip("/"),
        "USER": unquote(url.username or ""), "PASSWORD": unquote(url.password or ""),
        "HOST": url.hostname, "PORT": url.port,
        "OPTIONS": {"options": parse_qs(url.query)["options"][0]}}})
    import django
    django.setup()
spec = importlib.util.spec_from_file_location("schema_source", filename)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
if framework == "drf":
    from django.db import connection
    with connection.schema_editor() as editor:
        editor.create_model(module.Widget)
    connection.close()
else:
    from sqlalchemy import create_engine
    engine = create_engine(dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    module.Base.metadata.create_all(engine)
    engine.dispose()
"""


def schema_dsn(dsn: str, schema: str) -> str:
    parsed = urlsplit(dsn)
    query = [(key, value) for key, value in parse_qsl(parsed.query) if key != "options"]
    query.append(("options", "-csearch_path=" + schema))
    return urlunsplit(parsed._replace(query=urlencode(query)))


@pytest.mark.skipif(
    not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN") or os.getenv("SANKA_GO_TESTS") != "1",
    reason="requires explicit test PostgreSQL DSN and SANKA_GO_TESTS=1",
)
@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
def test_postgres_schema_parity(tmp_path: Path, framework: str, target: str) -> None:
    import psycopg
    from psycopg import sql

    output = generate(tmp_path, framework, target)
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    schemas = ["go_schema_" + uuid.uuid4().hex for _ in range(4)]
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            for schema in schemas:
                admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            source_dsn, candidate_dsn, occupied_dsn, failed_dsn = [
                schema_dsn(dsn, schema) for schema in schemas
            ]
            source_result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    SOURCE_DDL,
                    framework,
                    str(tmp_path / "models.py"),
                    source_dsn,
                ],
                text=True,
                capture_output=True,
                timeout=30,
            )
            assert source_result.returncode == 0, source_result.stderr
            environment = os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"}
            binary = tmp_path / ".sanka" / "migrate"
            subprocess.run(
                ["go", "build", "-mod=readonly", "-p=2", "-o", str(binary), "./cmd/migrate"],
                cwd=output,
                env=environment,
                check=True,
                capture_output=True,
                timeout=180,
            )

            def migrate(url: str, direction: str, *, success: bool = True) -> None:
                result = subprocess.run(
                    [str(binary), direction],
                    env=environment | {"DATABASE_URL": url},
                    text=True,
                    capture_output=True,
                    timeout=60,
                )
                assert (result.returncode == 0) is success, result.stderr

            migrate(candidate_dsn, "up")
            migrate(candidate_dsn, "up")

            def columns(schema: str) -> list:
                return admin.execute(
                    "SELECT column_name, data_type, is_nullable, character_maximum_length, "
                    "is_identity FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name='widgets' "
                    "ORDER BY ordinal_position",
                    (schema,),
                ).fetchall()

            assert columns(schemas[0]) == columns(schemas[1])
            # Actual inserts exercise identity, range, nullable and uniqueness semantics.
            for url in (source_dsn, candidate_dsn):
                with psycopg.connect(url, autocommit=True) as connection:
                    row = connection.execute(
                        "INSERT INTO widgets (name,count,enabled,note) "
                        "VALUES ('日本語',2147483647,false,NULL) RETURNING id,count,enabled,note"
                    ).fetchone()
                    assert row == (1, 2147483647, False, None)
                    with pytest.raises(psycopg.errors.UniqueViolation):
                        connection.execute(
                            "INSERT INTO widgets (name,count,enabled) VALUES ('日本語',1,true)"
                        )
                    with pytest.raises(psycopg.errors.NumericValueOutOfRange):
                        connection.execute(
                            "INSERT INTO widgets (name,count,enabled) "
                            "VALUES ('range',2147483648,true)"
                        )
                    with pytest.raises(psycopg.errors.NotNullViolation):
                        connection.execute(
                            "INSERT INTO widgets (name,count,enabled) VALUES ('null',1,NULL)"
                        )
                    with pytest.raises(psycopg.errors.StringDataRightTruncation):
                        connection.execute(
                            "INSERT INTO widgets (name,count,enabled) VALUES (%s,1,true)",
                            ("x" * 41,),
                        )
            migrate(candidate_dsn, "down")
            assert columns(schemas[1]) == []
            migrate(candidate_dsn, "up")
            assert columns(schemas[1]) == columns(schemas[0])
            with psycopg.connect(occupied_dsn, autocommit=True) as occupied:
                occupied.execute("CREATE TABLE sentinel (id integer PRIMARY KEY)")
                occupied.execute("INSERT INTO sentinel VALUES (7)")
                migrate(occupied_dsn, "up", success=False)
                assert occupied.execute("SELECT id FROM sentinel").fetchall() == [(7,)]
                assert columns(schemas[2]) == []
            if framework == "drf" and target == "fiber":
                # Force failure after CREATE TABLE to prove transactional rollback.
                migration = output / "migrations/00001_initial.sql"
                migration.write_text(
                    migration.read_text().replace("-- +goose Down", "SELECT 1/0;\n\n-- +goose Down")
                )
                subprocess.run(
                    ["go", "build", "-mod=readonly", "-p=2", "-o", str(binary), "./cmd/migrate"],
                    cwd=output,
                    env=environment,
                    check=True,
                    capture_output=True,
                    timeout=180,
                )
                migrate(failed_dsn, "up", success=False)
                assert columns(schemas[3]) == []
        finally:
            for schema in schemas:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )


@pytest.mark.parametrize(
    "option,value",
    [
        ("database_dialect", "sqlite"),
        ("schema_mode", "adopt-existing"),
        ("migration_tool", "unknown"),
        ("models_file", "../models.py"),
    ],
)
def test_database_profile_is_explicit(tmp_path: Path, option: str, value: str) -> None:
    (tmp_path / "app.py").write_text(source("flask"))
    req = request(tmp_path)
    req = dataclasses.replace(
        req, configuration=req.configuration | {"database_layer": "pgx", option: value}
    )
    assert handle(req).outcome == "error"


@pytest.mark.parametrize("framework", SOURCES)
def test_capture_matches_real_orm_metadata(tmp_path: Path, framework: str) -> None:
    path = tmp_path / "models.py"
    path.write_text(model_source(framework))
    probe = """
import json, sys
framework, filename = sys.argv[1:]
if framework == "drf":
    from django.conf import settings
    settings.configure(INSTALLED_APPS=[], DATABASES={"default": {
        "ENGINE": "django.db.backends.postgresql", "NAME": "unused"}})
    import django
    django.setup()
namespace = {"__name__": "schema_fixture"}
exec(compile(open(filename).read(), filename, "exec"), namespace)
if framework == "drf":
    from django.db import connection
    columns = [{"name": f.column, "sql_type": f.db_type(connection).lower(),
                "nullable": f.null, "primary_key": f.primary_key,
                "unique": bool(f.unique and not f.primary_key)}
               for f in namespace["Widget"]._meta.local_fields]
else:
    from sqlalchemy.dialects import postgresql
    columns = [{"name": f.name,
                "sql_type": str(f.type.compile(dialect=postgresql.dialect())).lower(),
                "nullable": f.nullable, "primary_key": f.primary_key,
                "unique": bool(f.unique and not f.primary_key)}
               for f in namespace["Widget"].__table__.columns]
print(json.dumps(columns))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, framework, str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    captured = capture_models(path, framework)[0]["fields"]
    expected = json.loads(result.stdout)
    assert [{key: field[key] for key in expected[0]} for field in captured] == expected


@pytest.mark.parametrize("table", ["goose_db_version", "goose_db_version_id_seq"])
def test_migration_bookkeeping_names_are_reserved(tmp_path: Path, table: str) -> None:
    path = tmp_path / "models.py"
    path.write_text(model_source("flask").replace("widgets", table))
    with pytest.raises(ValueError, match="migration bookkeeping"):
        capture_models(path, "flask")

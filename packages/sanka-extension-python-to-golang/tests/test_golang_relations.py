# SPDX-License-Identifier: Apache-2.0
"""Foreign-key capture preserves database behavior and rejects ORM cascades."""

import dataclasses
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sanka_extension_python_to_golang.database import render_database
from sanka_extension_python_to_golang.models import capture_models


def relational_source(framework: str) -> str:
    if framework == "drf":
        return """from django.db import models
class Parent(models.Model):
    id = models.BigAutoField(primary_key=True)
    class Meta:
        app_label = "catalog"
        db_table = "parents"
class Child(models.Model):
    id = models.BigAutoField(primary_key=True)
    parent = models.ForeignKey(Parent, on_delete=models.DO_NOTHING)
    class Meta:
        app_label = "catalog"
        db_table = "children"
"""
    return """from sqlalchemy import BigInteger, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
class Base(DeclarativeBase):
    pass
class Parent(Base):
    __tablename__ = "parents"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
class Child(Base):
    __tablename__ = "children"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    parent_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("parents.id"))
"""


def capture(tmp_path: Path, framework: str, text: str | None = None) -> list:
    path = tmp_path / "models.py"
    path.write_text(relational_source(framework) if text is None else text)
    return capture_models(path, framework)


@pytest.mark.parametrize("framework", ["drf", "flask", "fastapi"])
def test_relational_schema(tmp_path: Path, framework: str) -> None:
    models = capture(tmp_path, framework)
    assert [m["table"] for m in models] == ["parents", "children"]
    field = models[1]["fields"][1]
    assert field["name"] == "parent_id"
    assert field["sql_type"] == "bigint" and field["go_type"] == "int64"
    assert field["references"] == {
        "table": "parents",
        "column": "id",
        "on_delete": "NO ACTION",
        "deferrable": framework == "drf",
        "deferred": framework == "drf",
    }
    migration = render_database(
        {
            "models": models,
            "configuration": {
                "source_framework": framework,
                "schema_mode": "empty",
            },
        }
    )["migrations/00001_initial.sql"]
    assert migration.index('CREATE TABLE "parents"') < migration.index('CREATE TABLE "children"')
    assert migration.index('DROP TABLE "children"') < migration.index('DROP TABLE "parents"')
    assert 'REFERENCES "parents" ("id") ON DELETE NO ACTION' in migration
    assert ("DEFERRABLE INITIALLY DEFERRED" in migration) == (framework == "drf")
    assert ("CREATE INDEX" in migration) == (framework == "drf")


@pytest.mark.parametrize("action", ["CASCADE", "PROTECT", "SET_NULL", "RESTRICT"])
def test_django_python_deletion_policies_block(tmp_path: Path, action: str) -> None:
    with pytest.raises(ValueError, match="deletion"):
        capture(tmp_path, "drf", relational_source("drf").replace("DO_NOTHING", action))


@pytest.mark.parametrize(
    "replacement",
    [
        'ForeignKey("missing.id")',
        'ForeignKey("parents.missing")',
        'ForeignKey("public.parents.id")',
        'ForeignKey("parents.id", onupdate="CASCADE")',
        'ForeignKey("parents.id", ondelete="SET DEFAULT")',
        'ForeignKey("parents.id", ondelete="SET NULL")',
        'ForeignKey("parents.id", deferrable=False, initially="DEFERRED")',
    ],
)
def test_unknown_relationships_block(tmp_path: Path, replacement: str) -> None:
    with pytest.raises(ValueError):
        capture(
            tmp_path,
            "fastapi",
            relational_source("fastapi").replace('ForeignKey("parents.id")', replacement),
        )


@pytest.mark.parametrize("framework", ["drf", "flask", "fastapi"])
@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_relational_read_generation(tmp_path: Path, framework: str, target: str) -> None:
    from test_golang_reads import detail_source
    from test_golang_schema import generate

    source = detail_source(framework).replace("Widget", "Child")
    source = source.replace(
        '"id": item.id, "name": item.name, "count": item.count, '
        '"enabled": item.enabled, "note": item.note',
        '"id": item.id, "parent_id": item.parent_id',
    )
    output = generate(
        tmp_path, framework, target, app_source=source, model_text=relational_source(framework)
    )
    assert "ParentId int64" in (output / "models.go").read_text()
    assert "SELECT" in (output / "app.go").read_text()


@pytest.mark.skipif(
    not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN") or os.getenv("SANKA_GO_TESTS") != "1",
    reason="requires isolated PostgreSQL fixture and qualified Go toolchain",
)
@pytest.mark.parametrize(
    "framework,target,behavior",
    [
        (framework, target, "default")
        for framework in ("drf", "flask", "fastapi")
        for target in ("fiber", "chi", "mux", "gin")
    ]
    + [
        ("fastapi", "fiber", behavior)
        for behavior in ("CASCADE", "SET NULL", "RESTRICT", "deferred")
    ],
)
def test_relational_database_parity(
    tmp_path: Path, framework: str, target: str, behavior: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import psycopg
    from psycopg import sql
    from test_golang_schema import SOURCE_DDL, generate, schema_dsn

    model_text = relational_source(framework)
    if behavior != "default":
        option = (
            'deferrable=True, initially="DEFERRED"'
            if behavior == "deferred"
            else f'ondelete="{behavior}"'
        )
        model_text = model_text.replace(
            'ForeignKey("parents.id")', f'ForeignKey("parents.id", {option})'
        )
        if behavior == "SET NULL":
            model_text = model_text.replace(
                "parent_id: Mapped[int]", "parent_id: Mapped[int | None]"
            )
    output = generate(
        tmp_path,
        framework,
        target,
        model_text=model_text,
        app_source=related_read_source(framework),
    )
    environment = os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"}
    binary = tmp_path / "migrate"

    def build() -> None:
        result = subprocess.run(
            ["go", "build", "-mod=readonly", "-p=2", "-o", str(binary), "./cmd/migrate"],
            cwd=output,
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stderr

    def migrate(dsn: str, direction: str, success: bool = True) -> None:
        result = subprocess.run(
            [str(binary), direction],
            env=environment | {"DATABASE_URL": dsn},
            capture_output=True,
            timeout=60,
        )
        assert (result.returncode == 0) == success, "relational migration outcome differs"

    schemas = ["go_relations_" + uuid.uuid4().hex for _ in range(2)]
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            for schema in schemas:
                admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            source_dsn, target_dsn = [schema_dsn(dsn, schema) for schema in schemas]
            source_ddl = SOURCE_DDL.replace(
                "editor.create_model(module.Widget)",
                "editor.create_model(module.Parent)\n        editor.create_model(module.Child)",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    source_ddl,
                    framework,
                    str(tmp_path / "models.py"),
                    source_dsn,
                ],
                capture_output=True,
                timeout=30,
            )
            assert result.returncode == 0, "source relationship schema failed"
            build()
            migrate(target_dsn, "up")
            observations = []
            for url in (source_dsn, target_dsn):
                with psycopg.connect(url, autocommit=True) as connection:
                    foreign = connection.execute("""
                        SELECT c.relname, con.confdeltype, con.confupdtype,
                               con.condeferrable, con.condeferred
                        FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid
                        JOIN pg_namespace n ON n.oid=c.relnamespace
                        WHERE n.nspname=current_schema() AND con.contype='f'
                        ORDER BY c.relname
                    """).fetchall()
                    # Both tables roll back when the child violates its parent constraint.
                    with (
                        pytest.raises(psycopg.errors.ForeignKeyViolation),
                        connection.transaction(),
                    ):
                        connection.execute("INSERT INTO parents(id) VALUES (1)")
                        connection.execute("INSERT INTO children(id,parent_id) VALUES (1,99)")
                    assert connection.execute("SELECT * FROM parents").fetchall() == []
                    assert connection.execute("SELECT * FROM children").fetchall() == []
                    # Django defers the constraint to commit; SQLAlchemy defaults to immediate.
                    if framework == "drf" or behavior == "deferred":
                        with connection.transaction():
                            connection.execute("INSERT INTO children(id,parent_id) VALUES (1,1)")
                            connection.execute("INSERT INTO parents(id) VALUES (1)")
                    else:
                        with connection.transaction():
                            connection.execute("INSERT INTO parents(id) VALUES (1)")
                            connection.execute("INSERT INTO children(id,parent_id) VALUES (1,1)")
                    if behavior in {"CASCADE", "SET NULL"}:
                        connection.execute("DELETE FROM parents WHERE id=1")
                        rows = connection.execute("SELECT * FROM children").fetchall()
                        assert rows == ([] if behavior == "CASCADE" else [(1, None)])
                        connection.execute("INSERT INTO parents(id) VALUES (1)")
                        if behavior == "CASCADE":
                            connection.execute("INSERT INTO children(id,parent_id) VALUES (1,1)")
                        else:
                            connection.execute("UPDATE children SET parent_id=1 WHERE id=1")
                    else:
                        with pytest.raises(psycopg.errors.ForeignKeyViolation):
                            connection.execute("DELETE FROM parents WHERE id=1")
                    observations.append(
                        (
                            foreign,
                            connection.execute("SELECT * FROM parents ORDER BY id").fetchall(),
                            connection.execute("SELECT * FROM children ORDER BY id").fetchall(),
                        )
                    )
            assert observations[0] == observations[1]
            from sanka_extension_python_to_golang.adapter import handle
            from test_python_to_golang import request

            for url in (source_dsn, target_dsn):
                with psycopg.connect(url, autocommit=True) as connection:
                    connection.execute("INSERT INTO parents(id) VALUES (2)")
                    connection.execute(
                        "INSERT INTO children(id,parent_id) VALUES (2,2),(3,1),(4,1)"
                    )
            monkeypatch.setenv(
                "SANKA_GO_SOURCE_TEST_DATABASE_URL",
                source_dsn
                if framework == "drf"
                else source_dsn.replace("postgresql://", "postgresql+psycopg://", 1),
            )
            monkeypatch.setenv("SANKA_GO_TARGET_TEST_DATABASE_URL", target_dsn)
            req = request(tmp_path, framework, target)
            verified = handle(
                dataclasses.replace(
                    req,
                    command="verify",
                    configuration=req.configuration | {"database_layer": "pgx"},
                )
            )
            assert verified.outcome == "success", verified.error
            expected = [{"id": 1, "parent_id": 1}, {"id": 3, "parent_id": 1}]
            assert verified.data["source"][0]["body"] == expected
            assert verified.data["candidate"][0]["body"] == expected
            (output / "relation_transaction_test.go").write_text(RELATION_TRANSACTION)
            transaction = subprocess.run(
                ["go", "test", "-mod=readonly", "-p=2", "-run", "TestRelationTransaction", "."],
                cwd=output,
                env=environment | {"DATABASE_URL": target_dsn},
                capture_output=True,
                timeout=180,
            )
            assert transaction.returncode == 0, "pgx multi-table rollback failed"
            migrate(target_dsn, "down")
            migrate(target_dsn, "up")
            # Adoption must validate real ORM DDL and leave the populated source untouched.
            captured = {
                "models": capture_models(tmp_path / "models.py", framework),
                "configuration": {"source_framework": framework, "schema_mode": "adopt-existing"},
            }
            migration = output / "migrations/00001_initial.sql"
            migration.write_text(render_database(captured)["migrations/00001_initial.sql"])
            build()
            migrate(source_dsn, "up")
            migrate(source_dsn, "down")
            with psycopg.connect(source_dsn, autocommit=True) as connection:
                assert connection.execute("SELECT * FROM children ORDER BY id").fetchall() == [
                    (1, 1),
                    (2, 2),
                    (3, 1),
                    (4, 1),
                ]
                name = connection.execute("""
                    SELECT con.conname FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid
                    JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname=current_schema() AND con.contype='f'
                """).fetchone()[0]
                connection.execute(
                    sql.SQL("ALTER TABLE children DROP CONSTRAINT {}").format(sql.Identifier(name))
                )
                connection.execute(
                    "ALTER TABLE children ADD FOREIGN KEY(parent_id) "
                    "REFERENCES parents(id) ON DELETE SET DEFAULT"
                )
                migrate(source_dsn, "up", success=False)
                assert connection.execute("SELECT * FROM children ORDER BY id").fetchall() == [
                    (1, 1),
                    (2, 2),
                    (3, 1),
                    (4, 1),
                ]
        finally:
            for schema in schemas:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )


def related_read_source(framework: str) -> str:
    from test_golang_reads import detail_source

    source = detail_source(framework).replace("Widget", "Child")
    if framework == "drf":
        start = source.index("    item =")
        end = source.index("urlpatterns")
        source = (
            source[:start] + "    return Response(list(Child.objects.filter(parent_id=parent_id)"
            '.order_by("id").values("id", "parent_id")[:2]))\n' + source[end:]
        )
        return source.replace("request, id", "request, parent_id").replace(
            "<int:id>", "<int:parent_id>"
        )
    source = source.replace(
        "from sqlalchemy import create_engine", "from sqlalchemy import create_engine, select"
    )
    source = source[: source.index("        item =")]
    value = (
        "[dict(row) for row in session.execute(select(Child.id, Child.parent_id)"
        ".where(Child.parent_id == parent_id).order_by(Child.id).limit(2)).mappings()]"
    )
    source += (
        "        return " + ("jsonify(" + value + ")" if framework == "flask" else value) + "\n"
    )
    return (
        source.replace("get_widget(id", "get_widget(parent_id")
        .replace("<int:id>", "<int:parent_id>")
        .replace("{id}", "{parent_id}")
    )


@pytest.mark.parametrize("framework", ["drf", "flask", "fastapi"])
@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_related_list_generation(tmp_path: Path, framework: str, target: str) -> None:
    from test_golang_schema import generate

    output = generate(
        tmp_path,
        framework,
        target,
        app_source=related_read_source(framework),
        model_text=relational_source(framework),
    )
    app = (output / "app.go").read_text()
    assert "lookup int64" in app
    assert "ORDER BY" in app and "LIMIT $1" in app
    assert "readRows0" in app
    if os.getenv("SANKA_GO_TESTS") == "1":
        result = subprocess.run(
            ["go", "test", "-mod=readonly", "-p=2", "./..."],
            cwd=output,
            env=os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"},
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("mutation", ["cycle", "width", "unknown-option"])
def test_relational_graph_rejects_unsupported(tmp_path: Path, mutation: str) -> None:
    text = relational_source("fastapi")
    if mutation == "cycle":
        text = text.replace('ForeignKey("parents.id")', 'ForeignKey("children.id")')
    elif mutation == "width":
        text = text.replace("BigInteger, ForeignKey", "BigInteger, Integer, ForeignKey", 1)
        text = text.replace(
            "mapped_column(BigInteger, ForeignKey", "mapped_column(Integer, ForeignKey"
        )
    else:
        text = text.replace('ForeignKey("parents.id")', 'ForeignKey("parents.id", use_alter=True)')
    with pytest.raises(ValueError):
        capture(tmp_path, "fastapi", text)


def test_related_replay_paths() -> None:
    from sanka_extension_python_to_golang.replay import request_paths

    assert request_paths(
        {
            "path": "/children/:parent_id",
            "read": {
                "lookup": "parent_id",
                "many": True,
            },
        }
    ) == ["/children/1", "/children/2", "/children/2147483647"]


def test_relational_writes_stay_blocked(tmp_path: Path) -> None:
    from sanka_extension_python_to_golang.adapter import handle
    from test_golang_schema import model_source
    from test_golang_writes import fastapi_write_source
    from test_python_to_golang import request

    (tmp_path / "app.py").write_text(fastapi_write_source())
    models = model_source("fastapi").replace("import BigInteger,", "import ForeignKey, BigInteger,")
    relations = relational_source("fastapi")
    (tmp_path / "models.py").write_text(models + relations[relations.index("class Parent") :])
    req = request(tmp_path, "fastapi", "fiber")
    planned = handle(
        dataclasses.replace(
            req,
            configuration=req.configuration
            | {
                "database_layer": "pgx",
            },
        )
    )
    assert any("relational writes" in gap for gap in planned.data["capture"]["gaps"])
    assert planned.data["files"] == {}


def test_related_parameter_cannot_be_rebound_to_session(tmp_path: Path) -> None:
    from sanka_extension_python_to_golang.adapter import handle
    from test_python_to_golang import request

    (tmp_path / "models.py").write_text(
        relational_source("fastapi").replace("parent_id", "session")
    )
    (tmp_path / "app.py").write_text(related_read_source("fastapi").replace("parent_id", "session"))
    req = request(tmp_path, "fastapi", "fiber")
    planned = handle(
        dataclasses.replace(
            req,
            configuration=req.configuration
            | {
                "database_layer": "pgx",
            },
        )
    )
    assert planned.data["capture"]["gaps"] and planned.data["files"] == {}


RELATION_TRANSACTION = r"""package backend
import (
    "context"
    "errors"
    "os"
    "testing"
    "time"
    "github.com/jackc/pgx/v5"
    "github.com/jackc/pgx/v5/pgconn"
    "github.com/jackc/pgx/v5/pgxpool"
)
func TestRelationTransaction(t *testing.T) {
    ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
    defer cancel()
    config, err := pgxpool.ParseConfig(os.Getenv("DATABASE_URL"))
    if err != nil { t.Fatal("test database configuration failed") }
    config.MaxConns = 1
    pool, err := pgxpool.NewWithConfig(ctx, config)
    if err != nil { t.Fatal("test database unavailable") }
    defer pool.Close()
    rejected := errors.New("business operation rejected")
    for _, mode := range []string{"constraint", "work", "commit"} {
        err := pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {
            if _, err := tx.Exec(ctx, "INSERT INTO parents(id) VALUES (10)"); err != nil {
                return err
            }
            parent := 10
            if mode == "constraint" { parent = 999 }
            if _, err := tx.Exec(ctx,
                "INSERT INTO children(id,parent_id) VALUES (10,$1)", parent); err != nil {
                return err
            }
            if mode == "work" { return rejected }
            return nil
        })
        if mode == "constraint" {
            var pgError *pgconn.PgError
            if !errors.As(err, &pgError) || pgError.Code != "23503" {
                t.Fatal("foreign key error lost")
            }
        } else if mode == "work" {
            if !errors.Is(err, rejected) { t.Fatal("business error lost") }
        } else if err != nil { t.Fatal("commit failed") }
        var parents, children int
        if err := pool.QueryRow(ctx, "SELECT (SELECT count(*) FROM parents WHERE id=10), " +
            "(SELECT count(*) FROM children WHERE id=10)").Scan(&parents, &children); err != nil {
            t.Fatal("connection leaked after transaction")
        }
        expected := 0
        if mode == "commit" { expected = 1 }
        if parents != expected || children != expected { t.Fatal("partial transaction persisted") }
    }
}
"""

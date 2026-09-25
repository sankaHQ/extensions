# SPDX-License-Identifier: Apache-2.0
"""Bounded Alembic and relationship lowering, with four-router PostgreSQL replay."""

import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from sanka_extension_python_to_golang.capture import capture, configuration
from test_golang_relational_writes import backend_source, models_source, scenarios
from test_golang_schema import generate, schema_dsn


def project(root: Path, *, evolution: bool = False) -> str:
    model = models_source("fastapi")
    model = model.replace(
        "DeclarativeBase, Mapped, mapped_column",
        "DeclarativeBase, Mapped, mapped_column, relationship",
    )
    model = model.replace(
        '    __tablename__ = "parents"',
        '    __tablename__ = "parents"\n'
        '    widgets: Mapped[list["Widget"]] = relationship(back_populates="parent")',
    ).replace(
        '    parent_id: Mapped[int] = mapped_column(Integer, ForeignKey("parents.id"))',
        "    parent_id: Mapped[int] = mapped_column("
        'Integer, ForeignKey("parents.id"), index=True)\n'
        '    parent: Mapped[Parent] = relationship(back_populates="widgets")',
    )
    revisions = root / "alembic" / "versions"
    revisions.mkdir(parents=True)
    (revisions / "0001_parents.py").write_text("""from alembic import op
import sqlalchemy as sa
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None
def upgrade():
    op.create_table("parents",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(40), nullable=False, unique=True),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True))
def downgrade():
    op.drop_table("parents")
""")
    (revisions / "0002_widgets.py").write_text("""from alembic import op
import sqlalchemy as sa
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None
def upgrade():
    op.create_table("widgets",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(40), nullable=False, unique=True),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("parents.id"), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True))
    op.create_index("ix_widgets_parent_id", "widgets", ["parent_id"], unique=False)
def downgrade():
    op.drop_index("ix_widgets_parent_id", table_name="widgets")
    op.drop_table("widgets")
""")
    if evolution:
        second = revisions / "0002_widgets.py"
        second.write_text(
            second.read_text()
            .replace('        sa.Column("enabled", sa.Boolean(), nullable=False),\n', "")
            .replace('        sa.Column("note", sa.Text(), nullable=True))', "    )")
        )
        (revisions / "0003_widget_fields.py").write_text("""from alembic import op
import sqlalchemy as sa
revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None
def upgrade():
    op.add_column("widgets", sa.Column(
        "enabled", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("widgets", sa.Column("note", sa.Text(), nullable=True))
def downgrade():
    op.drop_column("widgets", "note")
    op.drop_column("widgets", "enabled")
""")
    return model


@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_linear_migrations_and_relationships_generate(tmp_path: Path, target: str) -> None:
    model = project(tmp_path)
    output = generate(
        tmp_path,
        "fastapi",
        target,
        app_source=backend_source("fastapi"),
        model_text=model,
    )
    assert sorted(path.name for path in (output / "migrations").iterdir()) == [
        "00001_0001.sql",
        "00002_0002.sql",
    ]
    assert 'CREATE TABLE "parents"' in (output / "migrations/00001_0001.sql").read_text()
    assert 'CREATE TABLE "widgets"' in (output / "migrations/00002_0002.sql").read_text()
    assert "DownTo(ctx, 0)" in (output / "database.go").read_text()
    assert (output / "tools/transfer_existing.py").is_file()


@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_linear_add_column_migration_generate(tmp_path: Path, target: str) -> None:
    model = project(tmp_path, evolution=True)
    output = generate(
        tmp_path,
        "fastapi",
        target,
        app_source=backend_source("fastapi"),
        model_text=model,
    )
    migrations = output / "migrations"
    assert sorted(path.name for path in migrations.iterdir()) == [
        "00001_0001.sql",
        "00002_0002.sql",
        "00003_0003.sql",
    ]
    assert '"enabled" boolean' not in (migrations / "00002_0002.sql").read_text()
    third = (migrations / "00003_0003.sql").read_text()
    assert 'ALTER TABLE "widgets" ADD COLUMN "enabled" boolean NOT NULL DEFAULT \'true\';' in third
    assert 'ALTER TABLE "widgets" ADD COLUMN "note" text;' in third
    assert third.index('DROP COLUMN "note"') < third.index('DROP COLUMN "enabled"')


@pytest.mark.parametrize(
    "before,after",
    [
        ('server_default="true"', 'server_default="maybe"'),
        ("sa.Boolean(), nullable=False", "sa.Integer(), nullable=False"),
        ('op.drop_column("widgets", "enabled")', 'op.drop_column("widgets", "note")'),
        ('op.add_column("widgets", sa.Column("note"', 'op.add_column("parents", sa.Column("note"'),
    ],
)
def test_add_column_mismatch_blocks(tmp_path: Path, before: str, after: str) -> None:
    model = project(tmp_path, evolution=True)
    (tmp_path / "app.py").write_text(backend_source("fastapi"))
    (tmp_path / "models.py").write_text(model)
    path = tmp_path / "alembic/versions/0003_widget_fields.py"
    path.write_text(path.read_text().replace(before, after))
    result = capture(
        tmp_path, configuration({"source_framework": "fastapi", "database_layer": "pgx"})
    )
    assert result["gaps"]


@pytest.mark.parametrize(
    "file,before,after",
    [
        ("0002_widgets.py", 'down_revision = "0001"', "down_revision = None"),
        ("0002_widgets.py", "sa.String(40)", "sa.String(80)"),
        ("0002_widgets.py", '["parent_id"]', '["enabled"]'),
        ("0002_widgets.py", '"ix_widgets_parent_id"', '"ix_widgets_other"'),
        ("0002_widgets.py", 'op.drop_table("widgets")', 'op.drop_table("parents")'),
        ("0002_widgets.py", 'op.create_table("widgets",', 'op.alter_column("widgets",'),
        ("0002_widgets.py", 'revision = "0002"', 'revision = "../escape"'),
        ("0002_widgets.py", "depends_on = None", 'depends_on = None\nprint("side effect")'),
    ],
)
def test_history_mismatch_blocks(tmp_path: Path, file: str, before: str, after: str) -> None:
    model = project(tmp_path)
    (tmp_path / "app.py").write_text(backend_source("fastapi"))
    (tmp_path / "models.py").write_text(model)
    path = tmp_path / "alembic" / "versions" / file
    path.write_text(path.read_text().replace(before, after))
    result = capture(
        tmp_path, configuration({"source_framework": "fastapi", "database_layer": "pgx"})
    )
    assert result["gaps"]


def test_unbacked_relationship_blocks(tmp_path: Path) -> None:
    model = project(tmp_path)
    (tmp_path / "app.py").write_text(backend_source("fastapi"))
    (tmp_path / "models.py").write_text(
        model.replace('ForeignKey("parents.id")', 'ForeignKey("widgets.id")')
    )
    result = capture(
        tmp_path, configuration({"source_framework": "fastapi", "database_layer": "pgx"})
    )
    assert any("models:" in gap for gap in result["gaps"])


@pytest.mark.skipif(
    os.getenv("SANKA_GO_TESTS") != "1" or not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN"),
    reason="requires isolated PostgreSQL and Go",
)
@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_source_to_go_postgres_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    import psycopg
    from psycopg import sql
    from sanka_extension_python_to_golang.replay import replay

    model = project(tmp_path, evolution=True)
    (tmp_path / "sanka-verify.json").write_text(json.dumps({"scenarios": scenarios()}))
    output = generate(
        tmp_path,
        "fastapi",
        target,
        app_source=backend_source("fastapi"),
        model_text=model,
    )
    captured = capture(
        tmp_path,
        configuration(
            {"source_framework": "fastapi", "target_framework": target, "database_layer": "pgx"}
        ),
    )
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    schemas = ["go_alembic_" + uuid.uuid4().hex for _ in range(2)]
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            for schema in schemas:
                admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            source, destination = [schema_dsn(dsn, schema) for schema in schemas]
            monkeypatch.setenv(
                "SANKA_GO_SOURCE_TEST_DATABASE_URL",
                source.replace("postgresql://", "postgresql+psycopg://", 1),
            )
            monkeypatch.setenv("SANKA_GO_TARGET_TEST_DATABASE_URL", destination)
            report = replay(tmp_path, output, captured, "verify")
            assert report["ok"], report["steps"]
            assert report["candidate"] == report["source"]
            indexes = [
                admin.execute(
                    "SELECT tablename,indexname FROM pg_indexes "
                    "WHERE schemaname=%s AND indexname NOT LIKE '%%_pkey' "
                    "ORDER BY tablename,indexname",
                    (schema,),
                ).fetchall()
                for schema in schemas
            ]
            assert indexes[0] == indexes[1]
            if target == "fiber":
                rolled_back = subprocess.run(
                    ["go", "run", "-mod=readonly", "./cmd/migrate", "down"],
                    cwd=output,
                    env=os.environ
                    | {"DATABASE_URL": destination, "GOTOOLCHAIN": "local", "GOWORK": "off"},
                    capture_output=True,
                    timeout=180,
                )
                assert rolled_back.returncode == 0, "generated migration rollback failed"
                assert admin.execute(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema=%s AND table_name IN ('parents','widgets')",
                    (schemas[1],),
                ).fetchone() == (0,)
        finally:
            for schema in schemas:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )


@pytest.mark.skipif(
    os.getenv("SANKA_GO_TESTS") != "1" or not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN"),
    reason="requires isolated PostgreSQL and Go",
)
def test_fastapi_existing_rows_transfer(tmp_path: Path) -> None:
    import runpy

    import psycopg
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from psycopg import sql
    from sqlalchemy import create_engine

    model = project(tmp_path, evolution=True)
    output = generate(
        tmp_path,
        "fastapi",
        "fiber",
        app_source=backend_source("fastapi"),
        model_text=model,
    )
    binary = tmp_path / "migrate-copy"
    built = subprocess.run(
        ["go", "build", "-mod=readonly", "-p=2", "-o", str(binary), "./cmd/migrate"],
        cwd=output,
        env=os.environ | {"GOWORK": "off", "GOTOOLCHAIN": "local", "GOMAXPROCS": "2"},
        capture_output=True,
        timeout=180,
    )
    assert built.returncode == 0, built.stderr.decode()
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    source_schema, target_schema = ["go_copy_" + uuid.uuid4().hex for _ in range(2)]
    with psycopg.connect(dsn, autocommit=True) as admin:
        for schema in (source_schema, target_schema):
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            source_dsn, target_dsn = [
                schema_dsn(dsn, schema) for schema in (source_schema, target_schema)
            ]
            engine = create_engine(source_dsn.replace("postgresql://", "postgresql+psycopg://", 1))
            try:
                with (
                    engine.begin() as connection,
                    Operations.context(MigrationContext.configure(connection)),
                ):
                    for revision in sorted((tmp_path / "alembic/versions").glob("*.py")):
                        if revision.name == "0003_widget_fields.py":
                            connection.exec_driver_sql(
                                "INSERT INTO parents(name,count,enabled) VALUES ('parent',2,true)"
                            )
                            connection.exec_driver_sql(
                                "INSERT INTO widgets(name,parent_id) VALUES ('widget',1)"
                            )
                        runpy.run_path(str(revision))["upgrade"]()
            finally:
                engine.dispose()
            with psycopg.connect(source_dsn, autocommit=True) as source:
                assert source.execute("SELECT enabled,note FROM widgets").fetchone() == (
                    True,
                    None,
                )
                source.execute("CREATE TABLE alembic_version(version_num text NOT NULL)")
                source.execute("INSERT INTO alembic_version VALUES ('0003')")
            migrated = subprocess.run(
                [str(binary), "up"],
                env=os.environ | {"DATABASE_URL": target_dsn},
                capture_output=True,
                timeout=60,
            )
            assert migrated.returncode == 0, migrated.stderr.decode()
            command = [os.sys.executable, str(output / "tools/transfer_existing.py")]
            environment = os.environ | {
                "SANKA_GO_SOURCE_DATABASE_URL": source_dsn,
                "DATABASE_URL": target_dsn,
            }

            def transfer(*args: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [*command, *args],
                    env=environment,
                    text=True,
                    capture_output=True,
                    timeout=60,
                )

            dry = transfer()
            assert dry.returncode == 0, dry.stderr
            assert json.loads(dry.stdout) == {
                "mode": "dry-run",
                "rows": {"parents": 1, "widgets": 1},
                "excluded_tables": ["alembic_version"],
            }
            unacknowledged = transfer("--execute")
            assert unacknowledged.returncode != 0
            assert "acknowledge-excluded-tables" in unacknowledged.stderr
            with psycopg.connect(target_dsn, autocommit=True) as target:
                target.execute("DELETE FROM goose_db_version WHERE version_id=3")
            partial = transfer()
            assert partial.returncode != 0
            assert "not fully applied" in partial.stderr
            with psycopg.connect(target_dsn, autocommit=True) as target:
                target.execute(
                    "INSERT INTO goose_db_version(version_id,is_applied) VALUES (3,true)"
                )
            copied = transfer("--execute", "--acknowledge-excluded-tables")
            assert copied.returncode == 0, copied.stderr
            verified = transfer("--verify")
            assert verified.returncode == 0, verified.stderr
            assert json.loads(verified.stdout)["rows"] == {"parents": 1, "widgets": 1}
            with psycopg.connect(target_dsn) as target:
                assert target.execute("SELECT enabled,note FROM widgets").fetchone() == (
                    True,
                    None,
                )
            with psycopg.connect(target_dsn, autocommit=True) as target:
                target.execute("ALTER TABLE widgets ALTER COLUMN enabled SET DEFAULT false")
            wrong_default = transfer("--verify")
            assert wrong_default.returncode != 0 and "columns differ" in wrong_default.stderr
            with psycopg.connect(target_dsn, autocommit=True) as target:
                target.execute("ALTER TABLE widgets ALTER COLUMN enabled SET DEFAULT true")
            again = transfer("--execute", "--acknowledge-excluded-tables")
            assert again.returncode != 0 and "nonempty" in again.stderr
            with psycopg.connect(source_dsn, autocommit=True) as source:
                source.execute("ALTER TABLE widgets DROP CONSTRAINT widgets_parent_id_fkey")
                source.execute(
                    "ALTER TABLE widgets ADD CONSTRAINT widgets_parent_id_fkey "
                    "FOREIGN KEY (parent_id) REFERENCES parents(id) ON DELETE CASCADE"
                )
            mismatched = transfer("--verify")
            assert mismatched.returncode != 0 and "constraints differ" in mismatched.stderr
        finally:
            for schema in (source_schema, target_schema):
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )

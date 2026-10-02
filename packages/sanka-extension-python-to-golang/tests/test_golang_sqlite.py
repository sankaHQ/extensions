# SPDX-License-Identifier: Apache-2.0
"""SQLite uses the same reviewed lifecycle and real HTTP replay as PostgreSQL."""

import ast
import dataclasses
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sanka_extension_python_to_golang.adapter import handle
from sanka_extension_python_to_golang.capture import capture, configuration
from sanka_extension_python_to_golang.render import render
from test_golang_drf_project import gadget_project, postgres_project
from test_golang_fastapi_migrations import project
from test_golang_reads import read_source
from test_golang_relational_writes import backend_source
from test_golang_schema import model_source, schema_dsn
from test_golang_write_parity import SCENARIOS
from test_golang_writes import fastapi_write_source, flask_write_source
from test_python_to_golang import request


def test_sqlite_plan_uses_captured_django_database(tmp_path: Path) -> None:
    config = gadget_project(tmp_path)
    config["database_layer"] = "sqlite"
    config.pop("database_dialect")
    planned = handle(dataclasses.replace(request(tmp_path, "drf", "chi"), configuration=config))
    assert planned.outcome == "success", planned.error
    assert planned.data["capture"]["configuration"]["database_dialect"] == "sqlite"
    assert planned.data["capture"]["gaps"] == []


@pytest.mark.skipif(os.getenv("SANKA_GO_TESTS") != "1", reason="requires Go toolchain")
@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_drf_sqlite_lifecycle_http_rows_identity_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    gadget_project(tmp_path)
    config = {"target_framework": target}
    original = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*.py")}
    monkeypatch.setenv("SANKA_GO_SOURCE_PYTHON", sys.executable)
    # Test/Verify provision their own disposable SQLite file; no container or user DB.
    monkeypatch.delenv("SANKA_GO_TARGET_TEST_DATABASE_URL", raising=False)
    base = dataclasses.replace(request(tmp_path, "drf", target), configuration=config)
    scanned = handle(dataclasses.replace(base, command="scan"))
    assert scanned.outcome == "success", scanned.error
    planned = handle(base)
    assert planned.outcome == "success", planned.error
    applied = handle(
        dataclasses.replace(
            base,
            command="apply",
            reviewed_plan_hash="reviewed-fixture",
            configuration=config | {"extension_plan_hash": planned.data["plan_hash"]},
        )
    )
    assert applied.outcome == "success", applied.error
    for command in ("test", "verify"):
        result = handle(dataclasses.replace(base, command=command))
        assert result.outcome == "success", result.error
        assert result.data["ok"]
        assert result.data["tests"] > 0
        put = next(e for e in result.data["endpoints"] if e["id"].startswith("PUT "))
        assert put["outcome"] == ("matched" if command == "verify" else "passed")
        assert put["scenarios"] == 3
        assert all(e["scenarios"] > 0 for e in result.data["endpoints"])
        assert len(result.data["endpoints"]) == 6
        assert result.data["environment"] == applied.data["output"]
        assert result.data["steps"] and all(not step["problems"] for step in result.data["steps"])
        progress = capsys.readouterr().err
        assert "Running Go tests" in progress
        assert progress.count("[sanka] MATCH " if command == "verify" else "[sanka] PASS ") >= len(
            result.data["steps"]
        )
        if command == "verify":
            assert "Collecting Python source responses" in progress
    assert {
        str(p.relative_to(tmp_path)): p.read_bytes()
        for p in tmp_path.rglob("*.py")
        if ".sanka" not in p.parts
    } == original


@pytest.mark.parametrize("framework", ["drf", "fastapi", "flask"])
def test_missing_source_framework_is_detected_without_importing_source(
    tmp_path: Path, framework: str
) -> None:
    from test_python_to_golang import source

    (tmp_path / "routes.py").write_text(source(framework))
    symbol = "urlpatterns" if framework == "drf" else "app"
    (tmp_path / "app.py").write_text(f"from routes import {symbol}\n")
    base = dataclasses.replace(request(tmp_path), configuration={"target": "chi"})
    planned = handle(base)
    assert planned.outcome == "success", planned.error
    assert planned.data["capture"]["configuration"]["source_framework"] == framework
    assert planned.data["capture"]["configuration"]["source_file"] == "app.py"
    assert not planned.data["capture"]["gaps"]


@pytest.mark.parametrize("postgres", [False, True])
def test_scan_discovers_django_source_before_destination_selection(tmp_path: Path, postgres: bool):
    (postgres_project if postgres else gadget_project)(tmp_path)
    base = dataclasses.replace(request(tmp_path), command="scan", configuration={})
    scanned = handle(base)
    assert scanned.outcome == "success", scanned.error
    config = scanned.data["configuration"]
    assert config["source_framework"] == "drf"
    assert config["source_file"] == ("shop_config/urls.py" if postgres else "crud_config/urls.py")
    assert config["models_file"] == ("orders/models.py" if postgres else "inventory/models.py")
    assert config["source_database"] == ("postgresql" if postgres else "sqlite")
    assert scanned.data["routes"] and not scanned.data["gaps"]
    plan = handle(
        dataclasses.replace(
            base, command="plan", configuration={"target": "chi", "database_layer": "pgx"}
        )
    )
    assert plan.outcome == "success", plan.error
    assert not plan.data["capture"]["gaps"]
    assert plan.data["capture"]["configuration"]["source_database"] == config["source_database"]
    assert plan.data["capture"]["configuration"]["database_layer"] == "pgx"
    assert handle(base).data == scanned.data
    if postgres:
        blocked = handle(
            dataclasses.replace(base, command="plan", configuration={"database_layer": "sqlite"})
        )
        assert blocked.outcome == "error"
    # Detection must still disclose source facts when generation has a semantic gap.
    settings = tmp_path / ("shop_config/settings.py" if postgres else "crud_config/settings.py")
    tree = ast.parse(settings.read_text())
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and ast.unparse(node.target) == "MIDDLEWARE":
            node.value = ast.parse("['custom.Middleware']", mode="eval").body
    settings.write_text(ast.unparse(tree))
    unsupported = handle(base)
    assert unsupported.outcome == "success", unsupported.error
    assert unsupported.data["configuration"]["source_database"] == config["source_database"]
    assert unsupported.data["gaps"] and not unsupported.data["generation_ready"]


@pytest.mark.parametrize("framework", ["fastapi", "flask"])
def test_scan_discovers_nested_sqlalchemy_source_without_executing_it(
    tmp_path: Path, monkeypatch, framework: str
):
    package = tmp_path / "backend"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "main.py").write_text(
        fastapi_write_source() if framework == "fastapi" else flask_write_source()
    )
    (package / "models.py").write_text(
        model_source(framework).replace("mapped_column(BigInteger,", "mapped_column(Integer,")
    )
    monkeypatch.setenv("DATABASE_URL", "sqlite:///source.db")
    base = dataclasses.replace(request(tmp_path), command="scan", configuration={})
    result = handle(base)
    assert result.outcome == "success", result.error
    config = result.data["configuration"]
    assert config["source_framework"] == framework
    assert config["source_file"] == "backend/main.py"
    assert config["models_file"] == "backend/models.py"
    assert config["source_database"] == "sqlite"
    assert config["database_layer"] == "sqlite"
    assert result.data["routes"] and not result.data["gaps"]
    assert not (tmp_path / "source.db").exists()
    monkeypatch.delenv("DATABASE_URL")
    unknown = handle(base)
    assert unknown.outcome == "error"
    assert unknown.error.code == "SANKA_EXTENSION_INPUT_REQUIRED"
    assert unknown.error.details["inputs"] == ["source_database"]
    unknown_plan = handle(
        dataclasses.replace(base, command="plan", configuration={"database_layer": "pgx"})
    )
    assert unknown_plan.outcome == "error"
    assert unknown_plan.error.details["inputs"] == ["source_database"]
    # An explicit source choice resolves an environment-only database without guessing.
    assert (
        handle(dataclasses.replace(base, configuration={"source_database": "sqlite"})).outcome
        == "success"
    )


def test_scan_requires_entrypoint_when_two_apps_are_present(tmp_path: Path):
    from test_python_to_golang import source

    (tmp_path / "app.py").write_text(source("flask"))
    (tmp_path / "main.py").write_text(source("fastapi"))
    base = dataclasses.replace(request(tmp_path), command="scan", configuration={})
    result = handle(base)
    assert result.outcome == "error"
    assert result.error.code == "SANKA_EXTENSION_INPUT_REQUIRED"
    assert result.error.details["inputs"] == ["source_file"]
    chosen = handle(dataclasses.replace(base, configuration={"source_file": "main.py"}))
    assert chosen.outcome == "success", chosen.error
    assert chosen.data["configuration"]["source_framework"] == "fastapi"


@pytest.mark.skipif(os.getenv("SANKA_GO_TESTS") != "1", reason="requires Go toolchain")
@pytest.mark.parametrize("framework", ["fastapi", "flask"])
@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
@pytest.mark.parametrize("target_layer", ["sqlite", "pgx"])
def test_sqlalchemy_sqlite_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, framework: str, target: str, target_layer: str
) -> None:
    if target_layer == "pgx" and not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN"):
        pytest.skip("requires disposable PostgreSQL fixture")
    (tmp_path / "app.py").write_text(
        fastapi_write_source() if framework == "fastapi" else flask_write_source()
    )
    (tmp_path / "models.py").write_text(
        model_source(framework).replace("mapped_column(BigInteger,", "mapped_column(Integer,")
    )
    (tmp_path / "sanka-verify.json").write_text(
        json.dumps(
            {
                "schema": "sanka.http-scenarios/v1",
                "scenarios": [
                    dict(
                        id=str(i),
                        expected_status=case["status"],
                        **{
                            k: v.replace("/api/", "/") if k == "path" else v
                            for k, v in case.items()
                            if k != "status"
                        },
                    )
                    for i, case in enumerate(SCENARIOS)
                ],
            }
        )
    )
    monkeypatch.setenv("SANKA_GO_SOURCE_PYTHON", sys.executable)
    base = dataclasses.replace(
        request(tmp_path, framework, target),
        configuration={
            "source_framework": "auto",
            "source_database": "sqlite",
            "database_layer": target_layer,
            "target": target,
        },
    )
    planned = handle(base)
    assert planned.outcome == "success", planned.error
    assert not planned.data["capture"]["gaps"], planned.data["capture"]["gaps"]
    applied = handle(
        dataclasses.replace(
            base,
            command="apply",
            reviewed_plan_hash="sqlite-fixture",
            configuration=base.configuration | {"extension_plan_hash": planned.data["plan_hash"]},
        )
    )
    assert applied.outcome == "success", applied.error
    admin = None
    if target_layer == "pgx":
        import psycopg
        from psycopg import sql

        admin = psycopg.connect(os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"], autocommit=True)
        schema = "sqlite_replay_" + uuid.uuid4().hex
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        monkeypatch.setenv(
            "SANKA_GO_TARGET_TEST_DATABASE_URL",
            schema_dsn(os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"], schema),
        )
        monkeypatch.setenv("SANKA_GO_ALLOW_DATABASE_RESET", "1")
    try:
        for command in ("test", "verify"):
            result = handle(dataclasses.replace(base, command=command))
            assert result.outcome == "success", result.error
            assert result.data["ok"], result.data
    finally:
        if admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            admin.close()


@pytest.mark.skipif(os.getenv("SANKA_GO_TESTS") != "1", reason="requires Go toolchain")
@pytest.mark.parametrize("framework", ["drf", "fastapi", "flask"])
def test_sqlite_read_only_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, framework: str
) -> None:
    (tmp_path / "app.py").write_text(read_source(framework))
    (tmp_path / "models.py").write_text(
        model_source(framework).replace("mapped_column(BigInteger,", "mapped_column(Integer,")
    )
    monkeypatch.setenv("SANKA_GO_SOURCE_PYTHON", sys.executable)
    base = dataclasses.replace(
        request(tmp_path, framework, "chi"),
        configuration={"source_framework": framework, "database_layer": "sqlite", "target": "chi"},
    )
    planned = handle(base)
    assert planned.outcome == "success", planned.error
    applied = handle(
        dataclasses.replace(
            base,
            command="apply",
            reviewed_plan_hash="fixture",
            configuration=base.configuration | {"extension_plan_hash": planned.data["plan_hash"]},
        )
    )
    assert applied.outcome == "success", applied.error
    for command in ("test", "verify"):
        result = handle(dataclasses.replace(base, command=command))
        assert result.outcome == "success", result.error


@pytest.mark.skipif(os.getenv("SANKA_GO_TESTS") != "1", reason="requires Go toolchain")
@pytest.mark.parametrize("target_layer", ["sqlite", "pgx"])
def test_sqlite_existing_rows_transfer_preserves_source_and_identity(
    tmp_path: Path, target_layer: str
) -> None:
    if target_layer == "pgx" and not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN"):
        pytest.skip("requires disposable PostgreSQL fixture")
    config = gadget_project(tmp_path)
    config.update(database_layer=target_layer, target_framework="chi")
    config.pop("database_dialect")
    captured = capture(tmp_path, configuration(config))
    assert captured["gaps"] == []
    output = tmp_path / ".sanka/candidate"
    for name, text in render(captured).items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    source_file = tmp_path / "source.sqlite3"
    environment = os.environ | {
        "SANKA_TEST_DB": str(source_file),
        "PYTHONPATH": str(tmp_path),
        "DJANGO_SETTINGS_MODULE": "crud_config.settings",
        "GOWORK": "off",
        "GOTOOLCHAIN": "local",
        "GOMAXPROCS": "2",
    }
    source = subprocess.run(
        [
            sys.executable,
            "-c",
            "import django; django.setup(); from django.core.management import call_command; "
            "call_command('migrate',interactive=False,verbosity=0)",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert source.returncode == 0, source.stderr
    with sqlite3.connect(source_file) as db:
        db.execute(
            "INSERT INTO inventory_gadget(id,name,quantity,notes) VALUES (7,'retained',12,'source')"
        )
        db.execute(
            "INSERT INTO inventory_gadget(id,name,quantity,notes) VALUES (101,'deleted',0,'')"
        )
        db.execute("DELETE FROM inventory_gadget WHERE id=101")
    source_digest = hashlib.sha256(source_file.read_bytes()).hexdigest()
    target_file = tmp_path / "target.sqlite3"
    schema, admin = "", None
    if target_layer == "pgx":
        import psycopg
        from psycopg import sql

        admin = psycopg.connect(os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"], autocommit=True)
        schema = "sqlite_copy_" + uuid.uuid4().hex
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        target_url = schema_dsn(os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"], schema)
    else:
        target_url = target_file.as_uri()
    environment |= {
        "SANKA_GO_SOURCE_DATABASE_URL": source_file.as_uri(),
        "DATABASE_URL": target_url,
    }
    try:
        if target_layer == "sqlite":
            with sqlite3.connect(target_file) as db:
                db.execute("CREATE TABLE existing(value INTEGER)")
                db.execute("INSERT INTO existing VALUES (7)")
            refused = subprocess.run(
                ["go", "run", "-mod=readonly", "-p=2", "./cmd/migrate", "up"],
                cwd=output,
                env=environment,
                capture_output=True,
                text=True,
                timeout=180,
            )
            assert refused.returncode != 0
            with sqlite3.connect(target_file) as db:
                assert db.execute("SELECT value FROM existing").fetchall() == [(7,)]
                db.execute("DROP TABLE existing")
        migrated = subprocess.run(
            ["go", "run", "-mod=readonly", "-p=2", "./cmd/migrate", "up"],
            cwd=output,
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert migrated.returncode == 0, migrated.stderr
        command = [sys.executable, str(output / "tools/transfer_existing.py")]
        dry = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=60)
        assert dry.returncode == 0, dry.stderr
        assert json.loads(dry.stdout)["rows"] == {"inventory_gadget": 1}
        refused = subprocess.run(
            [*command, "--execute"], env=environment, capture_output=True, text=True, timeout=60
        )
        assert refused.returncode != 0 and "acknowledge" in refused.stderr
        for flags in (["--execute", "--acknowledge-excluded-tables"], ["--verify"]):
            result = subprocess.run(
                [*command, *flags], env=environment, capture_output=True, text=True, timeout=60
            )
            assert result.returncode == 0, result.stderr
        repeated = subprocess.run(
            [*command, "--execute", "--acknowledge-excluded-tables"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert repeated.returncode != 0 and "nonempty" in repeated.stderr
        assert hashlib.sha256(source_file.read_bytes()).hexdigest() == source_digest
        rollback = subprocess.run(
            ["go", "run", "-mod=readonly", "-p=2", "./cmd/migrate", "down"],
            cwd=output,
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert rollback.returncode == 0, rollback.stderr
        if target_layer == "sqlite":
            with sqlite3.connect(target_file) as db:
                assert db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall() == [("goose_db_version",)]
    finally:
        if admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            admin.close()


@pytest.mark.skipif(os.getenv("SANKA_GO_TESTS") != "1", reason="requires Go toolchain")
@pytest.mark.parametrize("target_layer", ["sqlite", "pgx"])
def test_sqlite_relational_history_transfer_is_atomic(tmp_path: Path, target_layer: str) -> None:
    if target_layer == "pgx" and not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN"):
        pytest.skip("requires disposable PostgreSQL fixture")
    import runpy

    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine

    model = project(tmp_path).replace("mapped_column(BigInteger,", "mapped_column(Integer,")
    (tmp_path / "models.py").write_text(model)
    (tmp_path / "app.py").write_text(backend_source("fastapi"))
    revision = tmp_path / "alembic/versions/0002_widgets.py"
    revision.write_text(revision.read_text().replace("sa.BigInteger()", "sa.Integer()"))
    captured = capture(
        tmp_path,
        configuration(
            {
                "source_framework": "fastapi",
                "database_layer": target_layer,
                "source_database": "sqlite",
                "target_framework": "chi",
            }
        ),
    )
    assert captured["gaps"] == []
    output = tmp_path / ".sanka/generated"
    for name, content in render(captured).items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    source_file, target_file = tmp_path / "source.sqlite3", tmp_path / "target.sqlite3"
    engine = create_engine("sqlite:///" + str(source_file))
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            for path in sorted((tmp_path / "alembic/versions").glob("*.py")):
                runpy.run_path(str(path))["upgrade"]()
        connection.exec_driver_sql("INSERT INTO parents VALUES (7,'parent',3,1,NULL)")
        # A dangling child fails after its parent was copied: the destination must roll back both.
        connection.exec_driver_sql("INSERT INTO widgets VALUES (8,'child',99,1,NULL)")
    engine.dispose()
    admin = None
    if target_layer == "pgx":
        import psycopg
        from psycopg import sql

        admin = psycopg.connect(os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"], autocommit=True)
        schema = "sqlite_history_" + uuid.uuid4().hex
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        target_url = schema_dsn(os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"], schema)
    else:
        target_url = target_file.as_uri()
    environment = os.environ | {
        "DATABASE_URL": target_url,
        "SANKA_GO_SOURCE_DATABASE_URL": source_file.as_uri(),
        "GOTOOLCHAIN": "local",
        "GOWORK": "off",
        "GOMAXPROCS": "2",
    }
    try:

        def migrate(direction: str) -> None:
            result = subprocess.run(
                ["go", "run", "-mod=readonly", "-p=2", "./cmd/migrate", direction],
                cwd=output,
                env=environment,
                capture_output=True,
                text=True,
                timeout=180,
            )
            assert result.returncode == 0, result.stderr

        migrate("up")
        command = [sys.executable, str(output / "tools/transfer_existing.py"), "--execute"]
        failed = subprocess.run(
            command, env=environment, capture_output=True, text=True, timeout=60
        )
        assert failed.returncode != 0
        with (
            sqlite3.connect(target_file)
            if target_layer == "sqlite"
            else psycopg.connect(target_url) as db
        ):
            assert db.execute("SELECT count(*) FROM parents").fetchone() == (0,)
            assert db.execute("SELECT count(*) FROM widgets").fetchone() == (0,)
        with sqlite3.connect(source_file) as db:
            db.execute("UPDATE widgets SET parent_id=7")
        digest = hashlib.sha256(source_file.read_bytes()).hexdigest()
        for arguments in (
            command,
            [sys.executable, str(output / "tools/transfer_existing.py"), "--verify"],
        ):
            result = subprocess.run(
                arguments, env=environment, capture_output=True, text=True, timeout=60
            )
            assert result.returncode == 0, result.stderr
        assert hashlib.sha256(source_file.read_bytes()).hexdigest() == digest
        migrate("down")
        with (
            sqlite3.connect(target_file)
            if target_layer == "sqlite"
            else psycopg.connect(target_url) as db
        ):
            query = (
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                if target_layer == "sqlite"
                else "SELECT tablename FROM pg_tables WHERE schemaname=current_schema()"
            )
            assert db.execute(query).fetchall() == [("goose_db_version",)]
    finally:
        if admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            admin.close()

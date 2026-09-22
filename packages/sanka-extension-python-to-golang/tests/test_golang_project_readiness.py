# SPDX-License-Identifier: Apache-2.0
"""Project-root resolution and complete source accounting without executing tests."""

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sanka_extension_python_to_golang.adapter import handle
from sanka_extension_python_to_golang.capture import SOURCES, TARGETS, capture, configuration
from test_golang_project import packaged_source
from test_python_to_golang import PAYLOAD, request, source


def project(root: Path, framework: str, layout: str = "src") -> str:
    base = root / layout if layout else root
    base.mkdir(parents=True, exist_ok=True)
    if framework == "drf":
        (base / "service").mkdir()
        (base / "service/__init__.py").write_text("")
        (base / "service/main.py").write_text(source(framework))
        entry = "service/main.py"
    else:
        entry = packaged_source(base, framework)
    return (Path(layout) / entry).as_posix()


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("layout", ["", "src"])
def test_package_root_preserves_routes_and_snapshot(tmp_path: Path, framework: str, layout: str):
    entry = project(tmp_path, framework, layout)
    config = configuration({"source_framework": framework, "source_file": entry})
    result = capture(tmp_path, config)
    assert result["gaps"] == []
    assert result["generation_ready"] is True
    assert result["source_inventory"]["module_roles"]["application"] == sorted(
        [entry, *result["source_modules"]]
    )
    expected = {"fastapi": "/api/backend/v1/health", "flask": "/api/v1/health", "drf": "/health"}
    assert result["routes"][0]["path"] == expected[framework]
    assert capture(tmp_path, config) == result


@pytest.mark.parametrize("framework", SOURCES)
def test_source_tests_are_inventory_not_runtime_or_migrations(tmp_path: Path, framework: str):
    entry = project(tmp_path, framework)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/conftest.py").write_text("raise RuntimeError('never run source tests')")
    (tmp_path / "tests/test_api.py").write_text(
        "from pydantic import BaseModel\nclass Input(BaseModel):\n    custom: bytes\n"
    )
    config = configuration({"source_framework": framework, "source_file": entry})
    before = capture(tmp_path, config)
    assert before["gaps"] == []
    assert before["source_inventory"]["module_roles"]["tests"] == [
        "tests/conftest.py",
        "tests/test_api.py",
    ]
    (tmp_path / "tests/test_api.py").write_text("raise RuntimeError('also never run')")
    assert capture(tmp_path, config)["source_digest"] != before["source_digest"]
    (tmp_path / "worker.py").write_text("raise RuntimeError('uncaptured runtime')")
    result = capture(tmp_path, config)
    assert not result["generation_ready"]
    assert result["source_inventory"]["module_roles"]["unclassified"] == ["worker.py"]
    assert any("worker.py" in gap for gap in result["gaps"])


@pytest.mark.parametrize("change", ["initializer", "missing_init", "shadow", "import_test"])
def test_src_layout_does_not_hide_runtime_gaps(tmp_path: Path, change: str):
    entry = project(tmp_path, "fastapi")
    if change == "initializer":
        (tmp_path / "src/service/__init__.py").write_text("raise RuntimeError('never execute')")
    elif change == "missing_init":
        (tmp_path / "src/service/api/__init__.py").unlink()
    elif change == "shadow":
        (tmp_path / "src/fastapi.py").write_text("FastAPI = 1")
    else:
        (tmp_path / "src/service/test_helpers.py").write_text("def unsafe():\n    return 7\n")
        with (tmp_path / entry).open("a") as f:
            f.write("\nfrom .test_helpers import unsafe\n")
    result = capture(tmp_path, configuration({"source_framework": "fastapi", "source_file": entry}))
    assert result["gaps"]
    assert not result["generation_ready"]


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
def test_src_project_public_protocol_lifecycle(tmp_path: Path, framework: str, target: str):
    if os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("requires native Go")
    from sanka_extensions.code import encode_request

    entry = project(tmp_path, framework)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_original.py").write_text("raise RuntimeError('must not execute')")
    req = dataclasses.replace(
        request(tmp_path, framework, target),
        configuration={
            "source_framework": framework,
            "target_framework": target,
            "source_file": entry,
        },
    )

    def invoke(command, **changes):
        current = dataclasses.replace(req, command=command, **changes)
        process = subprocess.run(
            [sys.executable, "-m", "sanka_extension_python_to_golang"],
            input=json.dumps(encode_request(current)) + "\n",
            text=True,
            capture_output=True,
            timeout=180,
            env=os.environ | {"GOMAXPROCS": "2"},
        )
        assert process.returncode == 0, process.stdout + process.stderr
        response = json.loads(process.stdout)
        assert response["outcome"] == "success", response
        return response

    scanned = invoke("scan")
    assert scanned["data"]["generation_ready"]
    planned = invoke("plan")
    assert invoke("plan")["data"] == planned["data"]
    invoke(
        "apply",
        reviewed_plan_hash="reviewed",
        configuration=req.configuration | {"extension_plan_hash": planned["data"]["plan_hash"]},
    )
    tested = invoke("test")
    assert "source" not in tested["data"]
    assert tested["data"]["qualification"]["source_compared"] is False
    verified = invoke("verify")
    assert verified["data"]["source"] == verified["data"]["candidate"]
    assert verified["data"]["source"][0]["body"] == (
        PAYLOAD if framework == "drf" else {"ok": True}
    )
    assert not verified["data"]["complete_backend"]
    assert verified["data"]["qualification"] == {
        "candidate_executed": True,
        "source_compared": True,
        "original_tests_executed": False,
        "cutover_qualified": False,
    }


@pytest.mark.parametrize("framework", ["drf", "flask", "fastapi", "fastapi-async"])
@pytest.mark.parametrize("target", TARGETS)
def test_src_database_project_review_and_replay(
    tmp_path: Path, monkeypatch, framework: str, target: str
):
    from test_golang_async_persistence import injected_source
    from test_golang_schema import model_source, schema_dsn
    from test_golang_writes import drf_write_source, fastapi_write_source, flask_write_source

    async_ = framework.endswith("-async")
    framework = framework.removesuffix("-async")
    package = tmp_path / "src/backend"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "models.py").write_text(model_source(framework))
    app = {"drf": drf_write_source, "flask": flask_write_source, "fastapi": fastapi_write_source}[
        framework
    ]()
    if async_:
        app = injected_source("class")
    (package / "main.py").write_text(app.replace("from models import", "from .models import"))
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_database.py").write_text(
        "raise RuntimeError('original tests must not run')"
    )
    body = {"name": "first", "count": 1, "enabled": True}
    scenarios = [
        {
            "id": "invalid",
            "method": "POST",
            "path": "/widgets",
            "body": body | {"count": "bad"},
            "expected_status": 422 if async_ else 400,
        },
        {
            "id": "create",
            "method": "POST",
            "path": "/widgets",
            "body": body,
            "expected_status": 201,
        },
        {
            "id": "patch",
            "method": "PATCH",
            "path": "/widgets/1",
            "body": {"count": 2, "note": None},
            "expected_status": 200,
        },
        {
            "id": "absent",
            "method": "PATCH",
            "path": "/widgets/1",
            "body": {},
            "expected_status": 200,
        },
        {
            "id": "replace",
            "method": "PUT",
            "path": "/widgets/1",
            "body": body | {"name": "changed"},
            "expected_status": 200,
        },
        {"id": "delete", "method": "DELETE", "path": "/widgets/1", "expected_status": 204},
        {"id": "missing", "method": "DELETE", "path": "/widgets/1", "expected_status": 404},
        {
            "id": "recreate",
            "method": "POST",
            "path": "/widgets",
            "body": body,
            "expected_status": 201,
        },
    ]
    (tmp_path / "sanka-verify.json").write_text(json.dumps({"scenarios": scenarios}))
    req = dataclasses.replace(
        request(tmp_path, framework, target),
        configuration={
            "source_framework": framework,
            "target_framework": target,
            "source_file": "src/backend/main.py",
            "models_file": "src/backend/models.py",
            "database_layer": "pgx",
        },
    )
    planned = handle(req)
    assert planned.outcome == "success", planned.error
    assert planned.data["capture"]["gaps"] == []
    assert planned.data["capture"]["generation_ready"]
    assert planned.data == handle(req).data
    applied = handle(
        dataclasses.replace(
            req,
            command="apply",
            reviewed_plan_hash="reviewed",
            configuration=req.configuration | {"extension_plan_hash": planned.data["plan_hash"]},
        )
    )
    assert applied.outcome == "success", applied.error
    assert capture(tmp_path, planned.data["capture"]["configuration"]) == planned.data["capture"]
    if os.getenv("SANKA_GO_TESTS") != "1" or not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN"):
        pytest.skip(
            "capture/apply passed; database replay requires explicit PostgreSQL fixture DSN and Go"
        )
    import uuid

    import psycopg
    from psycopg import sql

    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    schemas = ["project_" + uuid.uuid4().hex for _ in range(2)]
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            for name in schemas:
                admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
            source_url, target_url = [schema_dsn(dsn, name) for name in schemas]
            if framework != "drf":
                source_url = source_url.replace("postgresql://", "postgresql+psycopg://", 1)
            monkeypatch.setenv("SANKA_GO_SOURCE_TEST_DATABASE_URL", source_url)
            monkeypatch.setenv("SANKA_GO_TARGET_TEST_DATABASE_URL", target_url)
            verified = handle(dataclasses.replace(req, command="verify"))
            if verified.outcome != "success":
                report_path = tmp_path / ".sanka/go/verify.json"
                if report_path.exists():
                    print(report_path.read_text())
            assert verified.outcome == "success", verified.error
            assert verified.data["source"] == verified.data["candidate"]
            assert verified.data["qualification"]["source_compared"]
            assert not verified.data["qualification"]["original_tests_executed"]
            assert any(row["status"] == 201 for row in verified.data["candidate"])
            assert all("tables" in row and "sequences" in row for row in verified.data["candidate"])
        finally:
            for name in schemas:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name))
                )


def test_test_named_migration_remains_a_blocker(tmp_path: Path):
    entry = project(tmp_path, "fastapi")
    versions = tmp_path / "migrations/versions"
    versions.mkdir(parents=True)
    (versions / "test_seed.py").write_text("raise RuntimeError('migration')")
    result = capture(tmp_path, configuration({"source_framework": "fastapi", "source_file": entry}))
    assert not result["generation_ready"]
    assert (
        "migrations/versions/test_seed.py"
        in result["source_inventory"]["module_roles"]["unclassified"]
    )

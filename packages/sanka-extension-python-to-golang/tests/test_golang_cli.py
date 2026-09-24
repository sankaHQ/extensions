# SPDX-License-Identifier: Apache-2.0
"""Installed release wheels through the actual CLI, never an editable converter."""

import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from test_golang_native_temporal import temporal_project
from test_golang_schema import model_source, schema_dsn
from test_golang_writes import flask_write_source

ROOT = Path(__file__).resolve().parents[3]
PROJECT_SOURCES = (
    "drf",
    "flask",
    "fastapi",
    "fastapi-async",
    "drf-project",
    "drf-multiapp",
    "drf-crossapp",
    "drf-postgresql",
    "drf-linear",
    "drf-composed",
    "drf-queries",
    "drf-complete",
    "flask-complete",
    "fastapi-complete",
    "fastapi-async-complete",
)


def hashes(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not {".sanka", "__pycache__"}.intersection(p.relative_to(root).parts)
    }


@pytest.fixture(scope="module")
def installed_cli(tmp_path_factory):
    if os.getenv("SANKA_GO_CLI_TESTS") != "1":
        pytest.skip("requires explicit CLI release qualification (CI marketplace fixture)")
    if not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN"):
        pytest.fail("CLI qualification requires an isolated PostgreSQL fixture")
    from scripts.qualify_api_release import EXAMPLES_REVISION, installer

    examples = ROOT / "release/examples"
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=examples, text=True
    ).strip()
    assert revision == EXAMPLES_REVISION
    subprocess.run(["git", "diff", "--exit-code", "HEAD", "--"], cwd=examples, check=True)
    empty = tmp_path_factory.mktemp("cli-input")
    (empty / "app.py").write_text("")
    consumer = installer(examples, ROOT / "release/api-converters", None)
    report_path = ROOT / "release/go-project-acceptance.json"
    report = {
        "schema": "sanka.python-to-golang.cli-qualification/v1",
        "outcome": "running",
        "cases": [],
    }
    report_path.write_text(json.dumps(report))
    with consumer(
        example=empty,
        extension="python-to-golang",
        packages=[],
        targets=["fiber", "chi", "mux", "gin"],
        match_file="app.py",
    ) as candidate:
        # CLI 0.2.12 prints JSON with ensure_ascii=False. Escape lone surrogates
        # at stdout rather than corrupting or dropping their scenario values.
        candidate.env.update(
            SANKA_GO_SOURCE_PYTHON=sys.executable,
            GOMAXPROCS="2",
            GOFLAGS="-p=2",
            PYTHONIOENCODING="utf-8:backslashreplace",
        )
        yield candidate, report, report_path
        report["candidate"] = candidate.report
        report["outcome"] = (
            "passed"
            if len(report["cases"]) == len(PROJECT_SOURCES) * 4
            and all(c["outcome"] == "passed" for c in report["cases"])
            else "failed"
        )
        report_path.write_text(json.dumps(report, indent=2) + "\n")


def cli_project(root, framework, target):
    if framework.endswith("-complete"):
        from test_golang_complete_projects import complete_project

        return complete_project(root, framework.split("-")[0], target, "async" in framework)
    if framework == "drf-queries":
        from test_golang_drf_queries import query_project

        return query_project(root) | {"target_framework": target}
    if framework in {
        "drf-project",
        "drf-multiapp",
        "drf-crossapp",
        "drf-postgresql",
        "drf-linear",
        "drf-composed",
    }:
        from test_golang_drf_project import (
            composed_project,
            crossapp_project,
            linear_schema_project,
            multiapp_project,
            postgres_project,
            project,
        )

        factory = {
            "drf-project": project,
            "drf-multiapp": multiapp_project,
            "drf-crossapp": crossapp_project,
            "drf-postgresql": postgres_project,
            "drf-linear": linear_schema_project,
            "drf-composed": composed_project,
        }[framework]
        config = factory(root) | {"target_framework": target}
        scenario = root / "sanka-verify.json"
        document = json.loads(scenario.read_text())
        if framework == "drf-multiapp":
            document = json.loads(json.dumps(document).replace("/api/", "/shop/"))
            sales = json.loads(json.dumps(document["scenarios"]).replace("/shop/", "/sales/"))
            for case in sales:
                case["id"] = "sales-" + case["id"]
            document["scenarios"] += sales
        if framework == "drf-postgresql":
            document.pop("db_env", None)
        scenario.write_text(json.dumps(document))
        return config
    if framework != "flask":
        config = temporal_project(root, framework, target)
        if framework.startswith("fastapi"):
            package = root / "src/backend"
            source = (package / "main.py").read_text()
            source = source.replace(
                "from fastapi import FastAPI,", "from fastapi import APIRouter,"
            )
            source = source.replace(
                "app = FastAPI(exception_handlers={RequestValidationError: invalid_request})",
                "api = APIRouter()",
            ).replace("@app.", "@api.")
            (package / "routes.py").write_text(source)
            (package / "main.py").write_text("""from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from .routes import api, invalid_request
def create_app():
    app = FastAPI(exception_handlers={RequestValidationError: invalid_request})
    app.include_router(api)
    return app
app = create_app()
""")
        return config
    package = root / "src/backend"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "models.py").write_text(model_source("flask"))
    (package / "routes.py").write_text(
        flask_write_source()
        .replace("from models import", "from .models import")
        .replace("from flask import Flask,", "from flask import Blueprint,")
        .replace("app = Flask(__name__)", 'api = Blueprint("api", __name__)')
        .replace("@app.", "@api.")
    )
    (package / "main.py").write_text(
        """from flask import Flask
from .routes import api
def create_app():
    app = Flask(__name__)
    app.register_blueprint(api)
    return app
app = create_app()
"""
    )
    body = {"name": "first", "count": 1, "enabled": True}
    scenarios = [
        {
            "id": "invalid",
            "method": "POST",
            "path": "/widgets",
            "body": body | {"count": "bad"},
            "expected_status": 400,
        },
        {
            "id": "create",
            "method": "POST",
            "path": "/widgets",
            "body": body,
            "expected_status": 201,
        },
        {
            "id": "null",
            "method": "PATCH",
            "path": "/widgets/1",
            "body": {"note": None},
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
            "body": body | {"count": 2},
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
    (root / "sanka-verify.json").write_text(json.dumps({"scenarios": scenarios}))
    return {
        "source_framework": "flask",
        "target_framework": target,
        "source_file": "src/backend/main.py",
        "models_file": "src/backend/models.py",
        "database_layer": "pgx",
    }


@pytest.mark.parametrize("framework", PROJECT_SOURCES)
@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_installed_cli_database_lifecycle(tmp_path, installed_cli, framework, target):
    import psycopg
    from psycopg import sql

    candidate, report, report_path = installed_cli
    candidate.project = tmp_path
    config = cli_project(tmp_path, framework, target)
    # Installation locks belong to each project, even when the wheel cache is shared.
    candidate.cli("extension", "add", "sanka/python-to-golang", "--marketplace", "release")
    before = hashes(tmp_path)
    case = {"source": framework, "target": target, "outcome": "failed"}
    report["cases"].append(case)
    schemas = ["cli_" + uuid.uuid4().hex for _ in range(2)]
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            for name in schemas:
                admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
            source_url, target_url = [schema_dsn(dsn, name) for name in schemas]
            if not framework.startswith("drf"):
                source_url = source_url.replace("postgresql://", "postgresql+psycopg://", 1)
            candidate.env.update(
                SANKA_GO_SOURCE_TEST_DATABASE_URL=source_url,
                SANKA_GO_TARGET_TEST_DATABASE_URL=target_url,
            )

            def cli(*args):
                forwarded = []
                for name in (
                    "SANKA_GO_SOURCE_PYTHON",
                    "SANKA_GO_SOURCE_TEST_DATABASE_URL",
                    "SANKA_GO_TARGET_TEST_DATABASE_URL",
                    "GOMAXPROCS",
                    "GOFLAGS",
                ):
                    forwarded.extend(["--extension-env", name])
                return candidate.cli(*args, *forwarded)

            scanned = cli("scan", ".", "--extension-config", json.dumps(config))
            assert scanned["outcome"] == "success"
            planned = cli("plan", ".", "--to", target, "--extension-config", json.dumps(config))[
                "data"
            ]
            assert planned["capture"]["generation_ready"]
            assert planned["capture"]["configuration"]["target_framework"] == target
            assert (
                cli("plan", ".", "--to", target, "--extension-config", json.dumps(config))["data"][
                    "plan_hash"
                ]
                == planned["plan_hash"]
            )
            with pytest.raises(RuntimeError, match="SANKA_EXTENSION_PLAN_HASH_MISMATCH"):
                cli("apply", "--plan-hash", "sha256:" + "0" * 64)
            output = tmp_path / ".sanka/extensions/sanka/python-to-golang/golang"
            assert not output.exists()
            cli("apply", "--plan-hash", planned["plan_hash"])
            generated = hashes(output)
            assert generated == {
                name: hashlib.sha256(content.encode()).hexdigest()
                for name, content in planned["files"].items()
            }
            tested = cli("test")["data"]
            assert tested["ok"] and not tested["qualification"]["source_compared"]
            verified = cli("verify")["data"]
            assert verified["ok"] and verified["source"] == verified["candidate"]
            assert verified["qualification"]["source_compared"]
            observations = verified["candidate"]
            if "drf_project" in planned["capture"]:
                observations = [row for group in observations for row in group]
            assert all("tables" in row and "sequences" in row for row in observations)
            assert hashes(tmp_path) == before
            assert hashes(output) == generated
            case.update(
                outcome="passed",
                plan_hash=planned["plan_hash"],
                extension_plan_hash=planned["extension"]["plan_hash"],
                source_sha256=before,
                generated_sha256=generated,
                scenarios=verified["candidate"],
                qualification=verified["qualification"],
            )
        finally:
            for name in schemas:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name))
                )
            report_path.write_text(json.dumps(report, indent=2) + "\n")

# SPDX-License-Identifier: Apache-2.0
"""Whole package composition and lifecycle qualification."""

from pathlib import Path

import pytest
from sanka_extension_python_to_golang.capture import TARGETS, capture, configuration


def packaged_source(root: Path, framework: str = "fastapi") -> str:
    for name in ("service", "service/api"):
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "__init__.py").write_text("")
    (root / "service/settings.py").write_text('API_PREFIX = "/api"\n')
    if framework == "fastapi":
        (root / "service/api/routes.py").write_text("""from fastapi import APIRouter
leaf = APIRouter(prefix="/v1")
@leaf.get("/health")
def health():
    return {"ok": True}
""")
        (root / "service/api/router.py").write_text("""from fastapi import APIRouter
from .routes import leaf
api = APIRouter(prefix="/backend")
api.include_router(leaf)
""")
        entry = """from fastapi import FastAPI
from .api.router import api
from .settings import API_PREFIX
def create_app():
    app = FastAPI()
    app.include_router(api, prefix=API_PREFIX)
    return app
app = create_app()
"""
    else:
        (root / "service/api/routes.py").write_text("""from flask import Blueprint, jsonify
leaf = Blueprint("leaf", __name__, url_prefix="/v1")
@leaf.get("/health")
def health():
    return jsonify({"ok": True})
""")
        (root / "service/api/router.py").write_text("""from flask import Blueprint
from .routes import leaf
api = Blueprint("api", __name__, url_prefix="/backend")
api.register_blueprint(leaf)
""")
        entry = """from flask import Flask
from .api.router import api
from .settings import API_PREFIX
def create_app():
    app = Flask(__name__)
    app.register_blueprint(api, url_prefix=API_PREFIX)
    return app
app = create_app()
"""
    (root / "service/main.py").write_text(entry)
    return "service/main.py"


@pytest.mark.parametrize("framework", ["fastapi", "flask"])
@pytest.mark.parametrize("target", TARGETS)
def test_package_factory_and_nested_routers(tmp_path: Path, framework: str, target: str) -> None:
    entry = packaged_source(tmp_path, framework)
    config = configuration(
        {"source_framework": framework, "source_file": entry, "target_framework": target}
    )
    result = capture(tmp_path, config)
    assert result["gaps"] == []
    expected = "/api/backend/v1/health" if framework == "fastapi" else "/api/v1/health"
    assert result["routes"][0]["path"] == expected
    assert "service/api/__init__.py" in result["source_modules"]
    assert capture(tmp_path, config) == result
    (tmp_path / "service/settings.py").write_text('API_PREFIX = "/changed"\n')
    assert capture(tmp_path, config) != result


@pytest.mark.parametrize("change", ["initializer", "cycle", "side_effect", "missing", "shadow"])
def test_package_gaps_block_generation(tmp_path: Path, change: str) -> None:
    entry = packaged_source(tmp_path)
    if change == "initializer":
        (tmp_path / "service/__init__.py").write_text("raise RuntimeError('must not execute')")
    elif change == "cycle":
        with (tmp_path / "service/api/routes.py").open("a") as stream:
            stream.write("\nfrom ..main import app\n")
    elif change == "side_effect":
        (tmp_path / "service/settings.py").write_text("API_PREFIX = compute_prefix()\n")
    elif change == "missing":
        (tmp_path / "service/api/__init__.py").unlink()
    else:
        with (tmp_path / "service/main.py").open("a") as stream:
            stream.write("\nAPI_PREFIX = '/other'\n")
    result = capture(tmp_path, configuration({"source_framework": "fastapi", "source_file": entry}))
    assert result["gaps"]


@pytest.mark.parametrize("framework", ["fastapi", "flask"])
def test_package_source_probe(tmp_path: Path, framework: str) -> None:
    import json
    import subprocess
    import sys

    from sanka_extension_python_to_golang.replay import SOURCE_PROBE, _client_lifecycle

    entry = packaged_source(tmp_path, framework)
    path = "/api/backend/v1/health" if framework == "fastapi" else "/api/v1/health"
    destination = tmp_path / "observed.json"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _client_lifecycle(SOURCE_PROBE),
            framework,
            str(tmp_path / entry),
            json.dumps([path]),
            str(destination),
            "",
            "0",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(destination.read_text())[0]["body"] == {"ok": True}


LIFESPAN = """@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    try:
        yield
    finally:
        await engine.dispose()
"""


def lifespan_source() -> str:
    from test_golang_async_reads import async_read_backend

    return (
        "from contextlib import asynccontextmanager\nfrom sqlalchemy import text\n"
        + async_read_backend("factory").replace(
            "app = FastAPI(", LIFESPAN + "\napp = FastAPI(lifespan=lifespan, "
        )
    )


def test_lifespan_reuses_database_runtime(tmp_path: Path) -> None:
    from sanka_extension_python_to_golang.render import render
    from test_golang_validation import captured_source

    result = captured_source(tmp_path, "fastapi", text=lifespan_source())
    assert result["gaps"] == []
    assert result["application"]["lifespan"] == {
        "startup": "database-ping",
        "shutdown": "database-dispose",
    }
    generated = render(result)
    runtime = generated["cmd/api/main.go"]
    assert "pool.Ping(" in runtime and "defer pool.Close()" in runtime


@pytest.mark.parametrize(
    "before,after",
    [
        ("SELECT 1", "DELETE FROM widgets"),
        ("await engine.dispose()", "pass"),
        ("finally:", "except Exception:"),
        ("lifespan=lifespan", "lifespan=custom"),
    ],
)
def test_changed_lifespan_blocks(tmp_path: Path, before: str, after: str) -> None:
    from test_golang_validation import captured_source

    assert captured_source(tmp_path, "fastapi", text=lifespan_source().replace(before, after))[
        "gaps"
    ]


@pytest.mark.parametrize(
    "fail_startup,fail_request", [(False, False), (True, False), (False, True)]
)
def test_probe_enters_and_exits_lifespan(
    tmp_path: Path, fail_startup: bool, fail_request: bool
) -> None:
    import json
    import subprocess
    import sys

    from sanka_extension_python_to_golang.replay import SOURCE_PROBE, _client_lifecycle

    marker = tmp_path / "lifecycle.json"
    source = tmp_path / "app.py"
    source.write_text(f"""from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
@asynccontextmanager
async def lifespan(app):
    Path({str(marker)!r}).write_text("started")
    if {fail_startup!r}:
        raise RuntimeError("startup failed")
    try:
        yield
    finally:
        Path({str(marker)!r}).write_text("stopped")
app = FastAPI(lifespan=lifespan)
@app.get("/health")
def health():
    assert Path({str(marker)!r}).read_text() == "started"
    if {fail_request!r}:
        raise RuntimeError("request failed")
    return {{"ok": True}}
""")
    destination = tmp_path / "observed.json"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _client_lifecycle(SOURCE_PROBE),
            "fastapi",
            str(source),
            json.dumps(["/health"]),
            str(destination),
            "",
            "0",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if fail_startup or fail_request:
        if fail_request:
            assert marker.read_text() == "stopped"
        assert result.returncode != 0
        assert not destination.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert marker.read_text() == "stopped"


def packaged_backend(root: Path, target: str):
    import ast
    import dataclasses

    from sanka_extension_python_to_golang.adapter import handle
    from test_golang_async_reads import async_read_backend
    from test_golang_schema import model_source
    from test_python_to_golang import request

    entry = packaged_source(root)
    (root / "service/models.py").write_text(model_source("fastapi"))
    (root / "service/settings.py").write_text(
        'from os import environ\nDATABASE_URL = environ["DATABASE_URL"]\nAPI_PREFIX = "/api"\n'
    )
    tree = ast.parse(async_read_backend("factory"))
    database, routes = [], []
    for node in tree.body:
        if (
            isinstance(node, ast.ImportFrom)
            and node.module in {"os", "sqlalchemy.pool", "sqlalchemy.ext.asyncio"}
        ) or (
            isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) in {"engine", "sessions"}
        ):
            database.append(node)
        elif isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "app":
            routes.extend(ast.parse('leaf = APIRouter(prefix="/v1")').body)
        else:
            routes.append(node)
    db_text = ast.unparse(ast.Module(body=database, type_ignores=[]))
    db_text = db_text.replace("from os import environ", "from .settings import DATABASE_URL")
    db_text = db_text.replace("environ['DATABASE_URL']", "DATABASE_URL")
    (root / "service/database.py").write_text(db_text)
    route_text = ast.unparse(ast.Module(body=routes, type_ignores=[]))
    route_text = route_text.replace("from models import Widget", "from ..models import Widget")
    route_text = route_text.replace("@app.", "@leaf.")
    (root / "service/api/routes.py").write_text(
        "from fastapi import APIRouter\n"
        "from sqlalchemy.ext.asyncio import AsyncSession\n"
        "from ..database import engine, sessions\n" + route_text
    )
    (root / entry).write_text(
        """from contextlib import asynccontextmanager
from sqlalchemy import text
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from .database import engine
from .api.router import api
from .api.routes import invalid_request
from .settings import API_PREFIX
"""
        + LIFESPAN
        + """
def create_app():
    app = FastAPI(lifespan=lifespan, exception_handlers={RequestValidationError: invalid_request})
    app.include_router(api, prefix=API_PREFIX)
    return app
app = create_app()
"""
    )
    config = {"database_layer": "pgx", "source_file": entry, "models_file": "service/models.py"}
    req = request(root, "fastapi", target)
    req = dataclasses.replace(req, configuration=req.configuration | config)
    plan = handle(req)
    assert plan.outcome == "success", plan.error
    assert plan.data["capture"]["gaps"] == [], plan.data["capture"]["gaps"]
    applied = handle(
        dataclasses.replace(
            req,
            command="apply",
            reviewed_plan_hash="runtime-review",
            configuration=req.configuration | {"extension_plan_hash": plan.data["plan_hash"]},
        )
    )
    assert applied.outcome == "success", applied.error
    return Path(applied.data["output"]), plan.data["capture"]


@pytest.mark.parametrize("target", TARGETS)
def test_packaged_backend_capture(tmp_path: Path, target: str) -> None:
    output, captured = packaged_backend(tmp_path, target)
    assert len(captured["routes"]) == 9
    assert all(route["path"].startswith("/api/backend/v1/") for route in captured["routes"])
    assert captured["application"]["factory"] == "create_app"
    assert (output / "cmd/api/main.go").is_file()
    import os
    import subprocess

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


def test_read_scenarios_respect_unique_names() -> None:
    from test_golang_async_reads import read_scenarios

    creates = [case["body"]["name"] for case in read_scenarios() if case["method"] == "POST"]
    assert len(creates) == len(set(creates))


def test_lifespan_annotation_must_be_bound_before_definition(tmp_path: Path) -> None:
    from test_golang_validation import captured_source

    source = lifespan_source().replace(
        "from fastapi import FastAPI, HTTPException", "from fastapi import HTTPException"
    )
    source = source.replace("app = FastAPI(", "from fastapi import FastAPI\napp = FastAPI(")
    assert captured_source(tmp_path, "fastapi", text=source)["gaps"]


def test_factory_dependencies_must_precede_invocation(tmp_path: Path) -> None:
    import ast

    from test_golang_validation import captured_source, native_fastapi_schema_source

    tree = ast.parse(native_fastapi_schema_source())
    handler = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "invalid_request"
    )
    tree.body.remove(handler)
    index = next(
        i
        for i, n in enumerate(tree.body)
        if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "app"
    )
    constructor = ast.unparse(tree.body[index])
    tree.body[index : index + 1] = [
        *ast.parse(
            "def create_app():\n    " + constructor + "\n    return app\napp = create_app()"
        ).body,
        handler,
    ]
    assert captured_source(tmp_path, "fastapi", text=ast.unparse(tree))["gaps"]

# SPDX-License-Identifier: Apache-2.0
"""Composed applications qualify identity, response hooks and transaction services."""

import ast
import copy
import json
import os
import uuid

import pytest
from sanka_extension_python_to_golang.capture import capture, configuration
from test_golang_identity import identity_source
from test_golang_query_composition import composed_source
from test_golang_service_workflows import scoped_backend, scoped_scenarios, write_project


def headers_source(source, framework):
    if framework == "flask":
        return (
            source
            + """
@app.after_request
def response_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
"""
        )
    if framework == "fastapi":
        return (
            source
            + """
@app.middleware("http")
async def response_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
"""
        )
    return """from django.utils.decorators import decorator_from_middleware
class ResponseHeaders:
    def __init__(self, get_response):
        self.get_response = get_response
    def process_response(self, request, response):
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response
""" + source.replace("@api_view(", "@decorator_from_middleware(ResponseHeaders)\n@api_view(")


def complete_project(root, framework, target="fiber", asynchronous=False):
    tree = ast.parse(scoped_backend(framework))
    reads = ast.parse(
        identity_source(
            framework,
            composed_source(framework).replace("health", "search").replace("count", "parent_id"),
        )
    )
    imported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) for a in n.names}
    for node in reads.body:
        if isinstance(node, ast.ImportFrom):
            node.names = [a for a in node.names if a.name not in imported]
            if node.names:
                tree.body.insert(0, node)
        elif isinstance(node, ast.FunctionDef) and node.name == "search":
            tree.body.append(node)
    if framework == "drf":
        urls = next(
            n
            for n in tree.body
            if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "urlpatterns"
        )
        urls.value.elts.append(ast.parse("path('search', search)", mode="eval").body)
        tree.body.remove(urls)
        tree.body.append(urls)
    write_project(root, framework, asynchronous, source=ast.unparse(tree))
    models = root / "models.py"
    models.write_text(models.read_text().replace(", unique=True", ""))
    app = root / "app.py"
    app.write_text(headers_source(app.read_text(), framework))
    cases = scoped_scenarios()
    cases[2]["body"]["note"] = "memo"
    cases[4:4] = [
        {"id": f"search-{i}", "method": "GET", "path": path, "expected_status": status}
        for i, (path, status) in enumerate(
            [
                ("/search?q=fixture-tenant&note=memo&limit=1", 200),
                ("/search?q=fixture-tenant&note=memo&limit=1&offset=1", 200),
                ("/search?limit=0", 400),
                ("/search?offset=-1", 400),
                ("/search?q=x%27%20OR%20%271%27=%271", 200),
            ]
        )
    ]
    (root / "sanka-verify.json").write_text(json.dumps({"scenarios": cases}))
    return configuration(
        {"source_framework": framework, "target_framework": target, "database_layer": "pgx"}
    )


@pytest.mark.parametrize("framework", ["drf", "flask", "fastapi"])
def test_complete_project_capture(tmp_path, framework):
    result = capture(tmp_path, complete_project(tmp_path, framework))
    assert not result["gaps"], result["gaps"]
    assert result["security"]["native"]
    assert (
        result["security"]["success_headers"]
        == result["security"]["denied_headers"]
        == {"cache-control": "no-store", "x-content-type-options": "nosniff"}
    )
    transactions = [r["write"] for r in result["routes"] if r.get("write", {}).get("transaction")]
    assert len(transactions) == 4
    assert all(
        step["scope"] == {"name": "tenant"} for tx in transactions for step in tx["transaction"]
    )


@pytest.mark.parametrize("framework", ["drf", "flask", "fastapi"])
def test_native_response_hook_execution_cannot_disappear(tmp_path, framework):
    config = complete_project(tmp_path, framework)
    app = tmp_path / "app.py"
    tree = ast.parse(app.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
            "response_headers",
            "process_response",
        }:
            node.body.insert(-1, ast.parse("print('side effect')").body[0])
    app.write_text(ast.unparse(tree))
    assert capture(tmp_path, config)["gaps"]


@pytest.mark.parametrize(
    "framework,expected", [("drf", "no-store"), ("flask", "no-store"), ("fastapi", "private")]
)
def test_native_response_hook_order(tmp_path, framework, expected):
    config = complete_project(tmp_path, framework)
    path = tmp_path / "app.py"
    hook = (
        headers_source("", framework)
        .replace("response_headers", "second_headers")
        .replace("no-store", "private")
    )
    if framework == "drf":
        tree = ast.parse(path.read_text())
        first = next(
            n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ResponseHeaders"
        )
        second = copy.deepcopy(first)
        second.name = "SecondHeaders"
        for node in ast.walk(second):
            if isinstance(node, ast.Constant) and node.value == "no-store":
                node.value = "private"
        tree.body.insert(tree.body.index(first) + 1, second)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and any(
                ast.unparse(d).startswith("api_view(") for d in node.decorator_list
            ):
                node.decorator_list.insert(
                    1, ast.parse("decorator_from_middleware(SecondHeaders)", mode="eval").body
                )
        path.write_text(ast.unparse(tree))
    else:
        path.write_text(path.read_text() + hook)
    result = capture(tmp_path, config)
    assert not result["gaps"], result["gaps"]
    assert result["security"]["success_headers"]["cache-control"] == expected


@pytest.mark.skipif(
    os.getenv("SANKA_GO_TESTS") != "1" or not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN"),
    reason="requires owned Go/PostgreSQL fixtures",
)
@pytest.mark.parametrize(
    "framework,asynchronous",
    [("drf", False), ("flask", False), ("fastapi", False), ("fastapi", True)],
)
@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_complete_project_replay(tmp_path, monkeypatch, framework, asynchronous, target):
    import psycopg
    from psycopg import sql
    from sanka_extension_python_to_golang.render import render
    from sanka_extension_python_to_golang.replay import replay
    from test_golang_schema import schema_dsn

    config = complete_project(tmp_path, framework, target, asynchronous)
    captured = capture(tmp_path, config)
    assert not captured["gaps"], captured["gaps"]
    output = tmp_path / ".sanka/candidate"
    for name, content in render(captured).items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    schemas = ["complete_" + uuid.uuid4().hex for _ in range(2)]
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            for schema in schemas:
                admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            source, target_url = [schema_dsn(dsn, schema) for schema in schemas]
            if framework != "drf":
                source = source.replace("postgresql://", "postgresql+psycopg://", 1)
            monkeypatch.setenv("SANKA_GO_SOURCE_TEST_DATABASE_URL", source)
            monkeypatch.setenv("SANKA_GO_TARGET_TEST_DATABASE_URL", target_url)
            result = replay(tmp_path, output, captured, "verify")
            assert result["ok"], result.get("steps")
            assert result["source"] == result["candidate"]
            assert result["denied_writes_unchanged"]
            assert result["security_headers"]["ok"]
        finally:
            for schema in schemas:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )

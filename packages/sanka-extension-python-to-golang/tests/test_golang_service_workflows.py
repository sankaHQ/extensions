# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""Business workflows must retain explicit transaction and identity boundaries."""

import ast
import copy
import os
from pathlib import Path

import pytest
from sanka_extension_python_to_golang.capture import capture, configuration
from test_golang_mixed_transactions import representative_source
from test_golang_relational_writes import models_source


def async_workflow_source(source: str | None = None) -> str:
    tree = ast.parse(source or representative_source("fastapi"))

    class Async(ast.NodeTransformer):
        def visit_With(self, node):
            self.generic_visit(node)
            if ast.unparse(node.items[0].context_expr) == "Session(engine)":
                node.items[0].context_expr = ast.parse("AsyncSession(engine)", mode="eval").body
            return ast.AsyncWith(items=node.items, body=node.body, type_comment=None)

        def visit_Call(self, node):
            self.generic_visit(node)
            if (
                isinstance(node.func, ast.Attribute)
                and ast.unparse(node.func.value) == "session"
                and node.func.attr in {"get", "delete", "flush", "commit", "refresh", "execute"}
            ):
                return ast.Await(value=node)
            return node

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and any(
            isinstance(n, ast.With) for n in ast.walk(node)
        ):
            Async().visit(node)
            node.__class__ = ast.AsyncFunctionDef
    source = ast.unparse(ast.fix_missing_locations(tree))
    source = source.replace(
        "from sqlalchemy import create_engine, select",
        "from sqlalchemy import select\nfrom sqlalchemy.pool import NullPool",
    )
    source = source.replace(
        "from sqlalchemy import create_engine", "from sqlalchemy.pool import NullPool"
    )
    source = source.replace(
        "from sqlalchemy.orm import Session",
        "from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine",
    )
    return source.replace(
        "create_engine(environ['DATABASE_URL'])",
        "create_async_engine(environ['DATABASE_URL'], poolclass=NullPool)",
    )


def workflow_capture(root: Path, source: str, framework: str = "fastapi") -> dict:
    (root / "app.py").write_text(source)
    (root / "models.py").write_text(models_source(framework))
    return capture(root, configuration({"source_framework": framework, "database_layer": "pgx"}))


def service_source(framework: str, asynchronous: bool = False, source: str | None = None) -> str:
    tree = ast.parse(
        async_workflow_source(source)
        if asynchronous
        else source or representative_source(framework)
    )
    helpers = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in {
            "relocate",
            "revise",
            "erase",
            "restore",
        }:
            repo = copy.deepcopy(node)
            repo.name = "repository_" + node.name
            repo.decorator_list = []
            repo.args.defaults = []
            service = copy.deepcopy(repo)
            service.name = "service_" + node.name
            args = ", ".join(a.arg for a in node.args.args)
            prefix = "await " if asynchronous else ""
            service.body = ast.parse(f"return {prefix}{repo.name}({args})").body
            node.body = ast.parse(f"return {prefix}{service.name}({args})").body
            helpers += [repo, service]
    tree.body[0:0] = helpers
    return ast.unparse(tree)


def test_async_mixed_capture(tmp_path: Path) -> None:
    result = workflow_capture(tmp_path, async_workflow_source())
    assert result["gaps"] == []
    assert sum(bool(r.get("write", {}).get("transaction")) for r in result["routes"]) == 4


@pytest.mark.parametrize(
    "framework,asynchronous",
    [("drf", False), ("flask", False), ("fastapi", False), ("fastapi", True)],
)
def test_service_chain_capture(tmp_path: Path, framework: str, asynchronous: bool) -> None:
    result = workflow_capture(tmp_path, service_source(framework, asynchronous), framework)
    assert result["gaps"] == []
    assert sum(bool(r.get("write", {}).get("transaction")) for r in result["routes"]) == 4


def scoped_workflow_source(framework: str, mode: str = "erase") -> str:
    from test_golang_identity import identity_source
    from test_golang_mixed_transactions import mixed_handler
    from test_golang_relational_writes import backend_source

    tree = ast.parse(backend_source(framework))
    # Keep imports/setup and one transaction; scalar CRUD is qualified separately.
    tree.body = [n for n in tree.body if not isinstance(n, ast.FunctionDef)]
    if framework == "drf":
        tree.body = [
            n
            for n in tree.body
            if not (isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "urlpatterns")
        ]
        tree.body.insert(0, ast.parse("from django.db import transaction").body[0])
    text = mixed_handler(framework, mode)
    if mode == "revise":
        text = (
            text.replace("Widget", "Parent")
            .replace("parent_id", "count")
            .replace(str(-(2**63)), str(-(2**31)))
            .replace(str(2**63 - 1), str(2**31 - 1))
        )
    node = ast.parse(text).body[0]
    receiver = {"fastapi": "principal", "drf": "request.auth", "flask": "g.principal"}[framework]
    for item in ast.walk(node):
        if isinstance(item, ast.If) and ast.unparse(item.test) in {
            f"{name} is None" for name in ("removed", "parent", "changed", "checked")
        }:
            variable = ast.unparse(item.test).split()[0]
            item.test = ast.parse(
                f"{variable} is None or {variable}.name != {receiver}['tenant']", mode="eval"
            ).body
    data = "request.data" if framework == "drf" else "data"
    denied = {
        "drf": 'return Response({"error": "permission denied"}, status=403)',
        "flask": 'return jsonify({"error": "permission denied"}), 403',
        "fastapi": 'raise HTTPException(status_code=403, detail="permission denied")',
    }[framework]
    variables = {
        "erase": [],
        "relocate": ["created", "changed"],
        "restore": ["created"],
        "revise": ["changed"],
    }[mode]
    body = node.body[0].body
    index = next(i for i, n in enumerate(body) if isinstance(n, ast.With))
    body[index:index] = [
        ast.parse(
            f"if 'name' in {data}[{name!r}] and {data}[{name!r}]['name'] != {receiver}['tenant']:\n    {denied}"
        ).body[0]
        for name in variables
    ]
    tree.body.append(node)
    if framework == "drf":
        tree.body += ast.parse(f"urlpatterns = [path('{mode}', {mode})]").body
    return identity_source(framework, ast.unparse(ast.fix_missing_locations(tree)))


@pytest.mark.parametrize("framework", ["drf", "flask", "fastapi"])
def test_scoped_transaction_capture(tmp_path: Path, framework: str) -> None:
    result = workflow_capture(tmp_path, scoped_workflow_source(framework), framework)
    assert result["gaps"] == []
    steps = result["routes"][0]["write"]["transaction"]
    assert [step["scope"] for step in steps] == [{"name": "tenant"}] * 2


@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_scoped_transaction_sql(tmp_path: Path, target: str) -> None:
    from test_golang_schema import generate

    output = generate(
        tmp_path,
        "fastapi",
        target,
        app_source=scoped_workflow_source("fastapi"),
        model_text=models_source("fastapi"),
    )
    source = (output / "app.go").read_text()
    helper = source.split("func writeRow0(", 1)[1].split("\nfunc ", 1)[0]
    assert helper.count(r"AND \"name\" = $2") == 4
    assert 'principal["tenant"]' in helper


def test_transaction_cross_tenant_probe(tmp_path: Path) -> None:
    from sanka_extension_python_to_golang.row_security import scoped_replay_roles

    result = workflow_capture(tmp_path, scoped_workflow_source("fastapi"))
    roles, bodies = scoped_replay_roles(
        {
            "method": "POST",
            "path": "/erase",
            "expected_status": 201,
            "body": {"removed": {"id": 1}, "parent": {"id": 1}},
        },
        (),
        result["routes"],
    )
    assert [(name, status) for name, _, status in roles] == [("transaction-cross-tenant", 404)]
    assert bodies == {}


@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_workflow_go_compiles(tmp_path: Path, target: str) -> None:
    import os

    from sanka_extension_python_to_golang.replay import _run
    from test_golang_schema import generate

    if os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("requires Go")
    output = generate(
        tmp_path,
        "fastapi",
        target,
        app_source=scoped_workflow_source("fastapi"),
        model_text=models_source("fastapi"),
    )
    _run(["go", "test", "-p=2", "./..."], output)


@pytest.mark.parametrize(
    "mutation",
    [
        "unawaited",
        "await-add",
        "nested",
        "commit",
        "effect",
        "recursive",
        "rebound",
        "renamed",
        "unawaited-helper",
    ],
)
def test_workflow_unsupported(tmp_path: Path, mutation: str) -> None:
    source = service_source("fastapi", True)
    before, after = {
        "unawaited": ("await session.flush()", "session.flush()"),
        "await-add": ("session.add(created)", "await session.add(created)"),
        "nested": ("session.begin()", "session.begin_nested()"),
        "commit": ("await session.flush()", "await session.commit()"),
        "effect": ("result = {'ok': True}", "result = print('side effect')"),
        "recursive": (
            "return await repository_relocate(data)",
            "return await service_relocate(data)",
        ),
        "rebound": (
            "return await repository_relocate(data)",
            "repository_relocate = None\n    return await repository_relocate(data)",
        ),
        "renamed": (
            "return await repository_relocate(data)",
            "return await repository_relocate(other)",
        ),
        "unawaited-helper": (
            "return await repository_relocate(data)",
            "return repository_relocate(data)",
        ),
    }[mutation]
    assert before in source
    assert workflow_capture(tmp_path, source.replace(before, after))["gaps"]


def write_project(root: Path, framework: str, asynchronous: bool = False) -> None:
    tree = ast.parse(service_source(framework, asynchronous))
    repository = [
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        and n.name.startswith("repository_")
    ]
    services = [
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name.startswith("service_")
    ]
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom)]
    engine = [
        n for n in tree.body if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "engine"
    ]
    tree.body = [n for n in tree.body if n not in repository + services + engine]
    (root / "repository.py").write_text(
        ast.unparse(ast.Module(body=copy.deepcopy(imports + engine + repository), type_ignores=[]))
    )
    names = ", ".join(n.name for n in repository)
    (root / "services.py").write_text(
        f"from repository import {names}\n"
        + ast.unparse(ast.Module(body=services, type_ignores=[]))
    )
    names = ", ".join(n.name for n in services)
    if engine:
        tree.body.insert(0, ast.parse("from repository import engine").body[0])
    tree.body.insert(0, ast.parse(f"from services import {names}").body[0])
    (root / "app.py").write_text(ast.unparse(tree))
    (root / "models.py").write_text(models_source(framework))


@pytest.mark.parametrize(
    "framework,asynchronous",
    [("drf", False), ("flask", False), ("fastapi", False), ("fastapi", True)],
)
def test_multimodule_workflow_plan(tmp_path: Path, framework: str, asynchronous: bool) -> None:
    import dataclasses

    from sanka_extension_python_to_golang.adapter import handle
    from test_python_to_golang import request

    write_project(tmp_path, framework, asynchronous)
    req = request(tmp_path, framework)
    req = dataclasses.replace(req, configuration=req.configuration | {"database_layer": "pgx"})
    scanned = handle(dataclasses.replace(req, command="scan"))
    assert scanned.outcome == "success", scanned.error
    planned = handle(req)
    assert planned.outcome == "success", planned.error
    assert planned.data == handle(req).data
    approved = dataclasses.replace(
        req,
        command="apply",
        reviewed_plan_hash="fixture-review",
        configuration=req.configuration | {"extension_plan_hash": planned.data["plan_hash"]},
    )
    assert handle(approved).outcome == "success"
    path = tmp_path / "repository.py"
    path.write_text(path.read_text() + "\n# source changed\n")
    assert handle(dataclasses.replace(req, command="test")).outcome == "error"


@pytest.mark.parametrize("framework", ["drf", "flask", "fastapi"])
@pytest.mark.parametrize("mode", ["relocate", "restore", "revise"])
def test_scoped_transaction_writes(tmp_path: Path, framework: str, mode: str) -> None:
    result = workflow_capture(tmp_path, scoped_workflow_source(framework, mode), framework)
    assert result["gaps"] == []
    assert all(
        step["scope"] == {"name": "tenant"} for step in result["routes"][0]["write"]["transaction"]
    )


def scoped_backend(framework: str) -> str:
    from test_golang_identity import identity_source
    from test_golang_relational_writes import backend_source

    tree = ast.parse(identity_source(framework, backend_source(framework)))
    if framework == "drf":
        tree.body.insert(0, ast.parse("from django.db import transaction").body[0])
    for mode in ("relocate", "restore", "revise", "erase"):
        extra = ast.parse(scoped_workflow_source(framework, mode))
        node = next(n for n in extra.body if isinstance(n, ast.FunctionDef) and n.name == mode)
        tree.body.append(node)
        if framework == "drf":
            urls = next(
                n
                for n in tree.body
                if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "urlpatterns"
            )
            urls.value.elts.append(ast.parse(f"path('{mode}', {mode})", mode="eval").body)
            tree.body.remove(urls)
            tree.body.append(urls)
    return ast.unparse(tree)


@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_scoped_mixed_go(tmp_path: Path, target: str) -> None:
    import os

    from sanka_extension_python_to_golang.replay import _run
    from test_golang_schema import generate

    output = generate(
        tmp_path,
        "fastapi",
        target,
        app_source=service_source("fastapi", True, scoped_backend("fastapi")),
        model_text=models_source("fastapi"),
    )
    captured = capture(
        tmp_path,
        configuration(
            {"source_framework": "fastapi", "target_framework": target, "database_layer": "pgx"}
        ),
    )
    index = next(i for i, route in enumerate(captured["routes"]) if route["path"] == "/revise")
    (output / "workflow_guard_test.go").write_text(
        """package backend
import ("context"; "errors"; "testing")
func TestWorkflowGuard(t *testing.T) {
    ctx := context.WithValue(context.Background(), principalContextKey{}, map[string]string{"sub":"fixture-user", "tenant":"fixture-tenant"})
    if _, err := writeRowINDEX(ctx, nil, []byte(`{"changed":{"id":1,"name":"outsider"},"checked":{"id":1}}`)); !errors.Is(err, errScopeDenied) { t.Fatal(err) }
    if _, err := writeRowINDEX(ctx, nil, []byte(`{"changed":{"id":1,"name":null},"checked":{"id":1}}`)); !errors.Is(err, errInvalidWrite) { t.Fatal(err) }
    if _, err := writeRowINDEX(context.Background(), nil, []byte(`{}`)); !errors.Is(err, errScopeDenied) { t.Fatal(err) }
}
""".replace("INDEX", str(index))
    )
    if os.getenv("SANKA_GO_TESTS") == "1":
        _run(["go", "test", "-p=2", "./..."], output)


@pytest.mark.skipif(
    not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN") or os.getenv("SANKA_GO_TESTS") != "1",
    reason="requires isolated PostgreSQL fixtures and Go",
)
@pytest.mark.parametrize(
    "framework,asynchronous",
    [("drf", False), ("flask", False), ("fastapi", False), ("fastapi", True)],
)
@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_service_project_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, framework: str, asynchronous: bool, target: str
) -> None:
    import dataclasses
    import json
    import uuid

    import psycopg
    from psycopg import sql
    from sanka_extension_python_to_golang.adapter import handle
    from test_golang_mixed_transactions import backend_scenarios
    from test_golang_schema import schema_dsn
    from test_python_to_golang import request

    write_project(tmp_path, framework, asynchronous)
    (tmp_path / "sanka-verify.json").write_text(json.dumps({"scenarios": backend_scenarios()}))
    req = request(tmp_path, framework, target)
    req = dataclasses.replace(req, configuration=req.configuration | {"database_layer": "pgx"})
    for command in ("scan", "plan"):
        planned = handle(dataclasses.replace(req, command=command))
        assert planned.outcome == "success", planned.error
    approved = dataclasses.replace(
        req,
        command="apply",
        reviewed_plan_hash="fixture-review",
        configuration=req.configuration | {"extension_plan_hash": planned.data["plan_hash"]},
    )
    applied = handle(approved)
    assert applied.outcome == "success", applied.error
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    schemas = ["workflow_" + uuid.uuid4().hex for _ in range(2)]
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            for schema in schemas:
                admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            source, destination = [schema_dsn(dsn, schema) for schema in schemas]
            if framework != "drf":
                source = source.replace("postgresql://", "postgresql+psycopg://", 1)
            monkeypatch.setenv("SANKA_GO_SOURCE_TEST_DATABASE_URL", source)
            monkeypatch.setenv("SANKA_GO_TARGET_TEST_DATABASE_URL", destination)
            for command in ("test", "verify"):
                result = handle(dataclasses.replace(req, command=command))
                assert result.outcome == "success", result.error
                assert result.data["ok"]
            assert result.data["source"] == result.data["candidate"]
            assert result.data["integrity_failures_unchanged"]
        finally:
            for schema in schemas:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )


def scoped_scenarios() -> list[dict]:
    def parent(name):
        return {"name": name, "count": 0, "enabled": True}

    def child(name, key):
        return {"name": name, "parent_id": key, "enabled": True}

    own = "fixture-tenant"
    rows = [
        ("/parents", 201, parent(own)),
        ("/parents", 201, parent("foreign")),
        ("/widgets", 201, child(own, 1)),
        ("/widgets", 201, child("foreign", 2)),
        ("/erase", 404, {"removed": {"id": 1}, "parent": {"id": 2}}),
        ("/revise", 403, {"changed": {"id": 1, "name": "foreign"}, "checked": {"id": 1}}),
        ("/revise", 201, {"changed": {"id": 1, "note": "hello"}, "checked": {"id": 1}}),
        ("/revise", 201, {"changed": {"id": 1, "note": None}, "checked": {"id": 1}}),
        ("/revise", 201, {"changed": {"id": 1}, "checked": {"id": 1}}),
        ("/revise", 404, {"changed": {"id": 1, "count": 9}, "checked": {"id": 2}}),
        (
            "/relocate",
            201,
            {
                "created": parent(own),
                "changed": {"id": 1, "name": own, "enabled": True},
                "removed": {"id": 1},
            },
        ),
        ("/restore", 404, {"removed": {"id": 1}, "created": parent(own), "checked": {"id": 2}}),
        ("/erase", 201, {"removed": {"id": 1}, "parent": {"id": 4}}),
    ]
    return [
        {"id": str(i), "method": "POST", "path": path, "expected_status": status, "body": body}
        for i, (path, status, body) in enumerate(rows)
    ]


@pytest.mark.skipif(
    not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN") or os.getenv("SANKA_GO_TESTS") != "1",
    reason="requires isolated PostgreSQL fixtures and Go",
)
@pytest.mark.parametrize(
    "framework,asynchronous",
    [("drf", False), ("flask", False), ("fastapi", False), ("fastapi", True)],
)
@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_scoped_workflow_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, framework: str, asynchronous: bool, target: str
) -> None:
    import json
    import uuid

    import psycopg
    from psycopg import sql
    from sanka_extension_python_to_golang.replay import replay
    from test_golang_schema import generate, schema_dsn

    source_text = service_source(framework, asynchronous, scoped_backend(framework))
    # Tenant names deliberately repeat; ownership columns are not unique identifiers.
    model_text = models_source(framework).replace(", unique=True", "")
    (tmp_path / "sanka-verify.json").write_text(json.dumps({"scenarios": scoped_scenarios()}))
    output = generate(tmp_path, framework, target, app_source=source_text, model_text=model_text)
    captured = capture(
        tmp_path,
        configuration(
            {"source_framework": framework, "target_framework": target, "database_layer": "pgx"}
        ),
    )
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    schemas = ["scoped_workflow_" + uuid.uuid4().hex for _ in range(2)]
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            for schema in schemas:
                admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            source, destination = [schema_dsn(dsn, schema) for schema in schemas]
            if framework != "drf":
                source = source.replace("postgresql://", "postgresql+psycopg://", 1)
            monkeypatch.setenv("SANKA_GO_SOURCE_TEST_DATABASE_URL", source)
            monkeypatch.setenv("SANKA_GO_TARGET_TEST_DATABASE_URL", destination)
            result = replay(tmp_path, output, captured, "verify")
            assert result["ok"], result["steps"]
            assert result["source"] == result["candidate"]
            assert result["integrity_failures_unchanged"] and result["denied_writes_unchanged"]
            final = result["candidate"][-1]["tables"]
            assert [r["id"] for r in final["widgets"]] == ["2"]
            assert [r["id"] for r in final["parents"]] == [2]
            if framework == "fastapi" and asynchronous and target == "fiber":
                app = output / "app.go"
                text = app.read_text()
                index = next(
                    i for i, route in enumerate(captured["routes"]) if route["path"] == "/erase"
                )
                start = text.index(f"func writeRow{index}(")
                end = text.index("\nfunc ", start + 1)
                helper = text[start:end]
                predicate = r" AND \"name\" = $2"
                assert predicate in helper
                app.write_text(
                    text[:start]
                    + helper.replace(predicate, " AND $2::text IS NOT NULL")
                    + text[end:]
                )
                changed = replay(tmp_path, output, captured, "verify")
                assert not changed["ok"], "removing ownership predicates must fail replay"
        finally:
            for schema in schemas:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )


@pytest.mark.parametrize(
    "framework,asynchronous",
    [("drf", False), ("flask", False), ("fastapi", False), ("fastapi", True)],
)
def test_original_service_modules_execute_validation(
    tmp_path: Path, framework: str, asynchronous: bool
) -> None:
    import json
    import subprocess
    import sys

    from sanka_extension_python_to_golang.replay import SOURCE_PROBE
    from sanka_extension_python_to_golang.security import REPLAY_ENV

    write_project(tmp_path, framework, asynchronous)
    script = (
        SOURCE_PROBE[: SOURCE_PROBE.index("observed = []")]
        + """
headers = {"authorization": "Bearer " + os.environ["AUTH_WRITE_TOKEN"]}
for path in ("/relocate", "/revise", "/erase", "/restore"):
    if framework == "drf":
        response = client.post(path, data="{}", content_type="application/json", headers=headers)
    else:
        response = client.post(path, json={}, headers=headers)
    assert response.status_code == 400, (path, response.status_code)
"""
    )
    run = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            script,
            framework,
            str(tmp_path / "app.py"),
            json.dumps([]),
            str(tmp_path / "unused.json"),
            str(tmp_path / "models.py"),
            "0",
        ],
        env=os.environ
        | REPLAY_ENV
        | {"DATABASE_URL": "postgresql+psycopg://fixture:fixture@127.0.0.1:1/unused"},
        text=True,
        capture_output=True,
    )
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.parametrize(
    "mutation",
    ["missing-row", "missing-body", "different-claim", "nullable-owner", "principal-rebound"],
)
def test_transaction_policy_cannot_be_weakened(tmp_path: Path, mutation: str) -> None:
    source = scoped_workflow_source("fastapi", "relocate")
    if mutation == "missing-row":
        source = source.replace(" or removed.name != principal['tenant']", "")
    elif mutation == "missing-body":
        source = source.replace(
            "if 'name' in data['changed'] and data['changed']['name'] != principal['tenant']:",
            "if False:",
        )
    elif mutation == "different-claim":
        source = source.replace(
            "changed.name != principal['tenant']", "changed.name != principal['sub']"
        )
    elif mutation == "nullable-owner":
        source = source.replace(".name != principal", ".note != principal")
    else:
        source = source.replace("result = {'ok': True}", "principal = {}")
    result = workflow_capture(tmp_path, source)
    assert result["gaps"]


def test_workflow_does_not_capture_callers_argument_for_global(tmp_path: Path) -> None:
    source = service_source("fastapi")
    source = source.replace("def repository_relocate(data: dict):", "def repository_relocate():")
    source = source.replace("return repository_relocate(data)", "return repository_relocate()")
    source += "\n@app.get('/data')\ndef data():\n    return {'ok': True}\n"
    assert workflow_capture(tmp_path, source)["gaps"]


def test_cross_row_probe_requires_existing_row_using_changed_claim(tmp_path: Path) -> None:
    from sanka_extension_python_to_golang.row_security import scoped_replay_roles

    source = scoped_workflow_source("fastapi", "relocate").replace(
        "data['created']['name'] != principal['tenant']",
        "data['created']['name'] != principal['sub']",
    )
    result = workflow_capture(tmp_path, source)
    assert result["gaps"] == []
    case = {
        "method": "POST",
        "path": "/relocate",
        "expected_status": 201,
        "body": {
            "created": {"name": "fixture-user", "count": 0, "enabled": True},
            "changed": {"id": 1, "name": "fixture-tenant", "enabled": True},
            "removed": {"id": 1},
        },
    }
    roles, _ = scoped_replay_roles(case, (), result["routes"])
    assert "transaction-cross-row-sub" not in {name for name, _, _ in roles}
    assert "transaction-cross-row-tenant" in {name for name, _, _ in roles}


def test_new_record_lookup_is_not_a_cross_tenant_denial() -> None:
    from sanka_extension_python_to_golang.row_security import scoped_replay_roles

    scope = {"name": "tenant"}
    route = {
        "method": "POST",
        "path": "/create",
        "write": {
            "operation": "create",
            "scope": scope,
            "transaction": [
                {"input": "created", "scope": scope},
                {
                    "input": "checked",
                    "operation": "lookup",
                    "scope": scope,
                    "lookup_reference": {"step": 0, "field": "id"},
                },
            ],
        },
    }
    case = {
        "method": "POST",
        "path": "/create",
        "expected_status": 201,
        "body": {"created": {"name": "fixture-tenant"}, "checked": {}},
    }
    roles, bodies = scoped_replay_roles(case, (), [route])
    assert [(name, status) for name, _, status in roles] == [("transaction-cross-tenant", 403)]
    assert bodies == {}

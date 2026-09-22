# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""Representative CRUD backend with explicit mixed-operation transactions."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import uuid
from itertools import pairwise
from pathlib import Path
from textwrap import indent

import pytest
from sanka_extension_python_to_golang.capture import capture, configuration
from test_golang_filters import filter_source
from test_golang_relational_writes import backend_source, models_source
from test_golang_schema import generate, schema_dsn
from test_golang_security import secured_source

STEPS = {
    "relocate": [
        ("created", "Parent", "create"),
        ("changed", "Widget", "replace"),
        ("removed", "Parent", "delete"),
    ],
    "revise": [("changed", "Widget", "patch"), ("checked", "Parent", "lookup")],
    "erase": [("removed", "Widget", "delete"), ("parent", "Parent", "delete")],
    "restore": [
        ("removed", "Widget", "delete"),
        ("created", "Parent", "create"),
        ("checked", "Parent", "lookup"),
    ],
}


def input_fields(model: str, operation: str, *, referenced: bool = False) -> list[tuple]:
    fields = (
        [("id", "int", 64 if model == "Widget" else 32, False)] if operation != "create" else []
    )
    if operation in {"lookup", "delete"}:
        return fields
    fields += [("name", "str", 0, False)]
    if not referenced:
        fields += [("parent_id" if model == "Widget" else "count", "int", 32, False)]
    return [*fields, ("enabled", "bool", 0, False), ("note", "str", 0, True)]


def validation(data: str, fields: list[tuple], partial: bool, error: str) -> str:
    conditions = [
        f"type({data}) is not dict",
        f"set({data}) - {{{', '.join(repr(f[0]) for f in fields)}}}",
    ]
    for name, kind, bits, nullable in fields:
        value = f"{data}[{name!r}]"
        if not partial and not nullable:
            conditions.append(f"{name!r} not in {data}")
        invalid = f"type({value}) is not {kind}"
        if bits:
            invalid += f" or not {-(2 ** (bits - 1))} <= {value} <= {2 ** (bits - 1) - 1}"
        if nullable:
            invalid = f"{value} is not None and ({invalid})"
        if partial or nullable:
            invalid = f"{name!r} in {data} and ({invalid})"
        conditions.append(invalid)
    result = "if " + " or ".join(f"({c})" for c in conditions) + f":\n    {error}\n"
    if partial:
        result += f"if 'id' not in {data}:\n    {error}\n"
    return result


def mixed_handler(framework: str, mode: str) -> str:
    data = "request.data" if framework == "drf" else "data"
    invalid = {
        "drf": 'return Response({"error": "invalid request body"}, status=400)',
        "flask": 'return jsonify({"error": "invalid request body"}), 400',
        "fastapi": 'raise HTTPException(status_code=400, detail="invalid request body")',
    }[framework]
    steps = STEPS[mode]
    keys = "{" + ", ".join(repr(n) for n, _, _ in steps) + "}"
    prefix = "data = request.get_json()\n" if framework == "flask" else ""
    prefix += f"if type({data}) is not dict or set({data}) != {keys}:\n    {invalid}\n"
    work = []
    for name, model, op in steps:
        referenced = mode == "relocate" and op == "replace"
        body = f"{data}[{name!r}]"
        fields = input_fields(model, op, referenced=referenced)
        prefix += validation(body, fields, op == "patch", invalid)
        writable = input_fields(model, "create")

        def value(field: tuple, referenced: bool = referenced, body: str = body) -> str:
            if referenced and field[0] == "parent_id":
                return "created.id"
            return f"{body}.get({field[0]!r})" if field[3] else f"{body}[{field[0]!r}]"

        if op == "create":
            constructor = model + (".objects.create" if framework == "drf" else "")
            work.append(
                f"{name} = {constructor}({', '.join(f'{f[0]}={value(f)}' for f in writable)})"
            )
            if framework != "drf":
                work += [f"session.add({name})", "session.flush()"]
        else:
            lookup = (
                f"{model}.objects.filter(id={body}['id']).first()"
                if framework == "drf"
                else f"session.get({model}, {body}['id'])"
            )
            work += [
                f"{name} = {lookup}",
                f"if {name} is None:\n    raise LookupError('not found')",
            ]
            if op in {"replace", "patch"}:
                for field in writable:
                    if op == "patch":
                        work.append(
                            f"if {field[0]!r} in {body}:\n    {name}.{field[0]} = {body}[{field[0]!r}]"
                        )
                    else:
                        work.append(f"{name}.{field[0]} = {value(field)}")
                if framework == "drf":
                    update = (
                        f"[field for field in {body} if field != 'id']"
                        if op == "patch"
                        else repr([f[0] for f in writable])
                    )
                    work.append(f"{name}.save(update_fields={update})")
                else:
                    work.append("session.flush()")
            elif op == "delete":
                work.append(f"{name}.delete()" if framework == "drf" else f"session.delete({name})")
                if framework != "drf":
                    work.append("session.flush()")
    work.append("result = {'ok': True}")
    if framework == "drf":
        scope = "with transaction.atomic():\n" + indent("\n".join(work), "    ")
        result = "return Response(result, status=201)"
        decorators = "@api_view(['POST'])\n@authentication_classes([])\n@permission_classes([AllowAny])\n@renderer_classes([JSONRenderer])\n"
        args = "request"
    else:
        scope = "with Session(engine) as session:\n    with session.begin():\n" + indent(
            "\n".join(work), "        "
        )
        result = "return jsonify(result), 201" if framework == "flask" else "return result"
        decorators = (
            f"@app.post('/{mode}'" + (", status_code=201" if framework == "fastapi" else "") + ")\n"
        )
        args = "data: dict" if framework == "fastapi" else ""
    conflict = invalid.replace("invalid request body", "integrity conflict").replace("400", "409")
    missing = invalid.replace("invalid request body", "not found").replace("400", "404")
    body = (
        "try:\n"
        + indent(prefix + scope + "\n" + result, "    ")
        + f"\nexcept IntegrityError:\n    {conflict}\nexcept LookupError:\n    {missing}\n"
    )
    return decorators + f"def {mode}({args}):\n" + indent(body, "    ")


def representative_source(framework: str) -> str:
    base = ast.parse(backend_source(framework))
    if framework == "drf":
        base.body.insert(0, ast.parse("from django.db import transaction").body[0])
    for mode in STEPS:
        base.body += ast.parse(mixed_handler(framework, mode)).body
    reads = ast.parse(
        filter_source(framework).replace("health", "search").replace("count", "parent_id")
    )
    imported = {
        alias.name for node in base.body if isinstance(node, ast.ImportFrom) for alias in node.names
    }
    for node in reads.body:
        if isinstance(node, ast.ImportFrom):
            node.names = [alias for alias in node.names if alias.name not in imported]
            if node.names:
                base.body.insert(0, node)
        elif isinstance(node, ast.FunctionDef):
            base.body.append(node)
    if framework == "drf":
        urls = next(
            node
            for node in base.body
            if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "urlpatterns"
        )
        base.body.remove(urls)
        assert isinstance(urls.value, ast.List)
        urls.value.elts += [
            ast.parse(f"path('{mode}', {mode})", mode="eval").body for mode in STEPS
        ]
        urls.value.elts.append(ast.parse("path('search', search)", mode="eval").body)
        base.body.append(urls)
    return secured_source(framework, ast.unparse(base))


@pytest.mark.parametrize("framework", ["drf", "flask", "fastapi"])
@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_mixed_backend_capture(tmp_path: Path, framework: str, target: str) -> None:
    output = generate(
        tmp_path,
        framework,
        target,
        app_source=representative_source(framework),
        model_text=models_source(framework),
    )
    captured = capture(
        tmp_path,
        configuration(
            {"source_framework": framework, "target_framework": target, "database_layer": "pgx"}
        ),
    )
    operations = {
        step.get("operation", "create")
        for route in captured["routes"]
        for step in route.get("write", {}).get("transaction", [])
    }
    assert operations == {"create", "replace", "patch", "delete", "lookup"}
    text = (output / "app.go").read_text()
    relocate = captured["routes"].index(
        next(route for route in captured["routes"] if route["path"] == "/relocate")
    )
    helper = text.split(f"func writeRow{relocate}(", 1)[1].split("\nfunc ", 1)[0]
    assert "UPDATE" in helper and "DELETE" in helper and "SELECT" in helper
    assert helper.count("pgx.BeginFunc") == 1
    if os.getenv("SANKA_GO_TESTS") == "1":
        run = subprocess.run(
            ["go", "test", "-mod=readonly", "-p=2", "./..."],
            cwd=output,
            env=os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"},
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        "return-missing",
        "wrong-catch",
        "nested",
        "commit",
        "missing-id",
        "partial-null",
        "scope-removed",
        "side-effect",
        "shadow",
    ],
)
def test_mixed_unsupported_semantics_block(tmp_path: Path, mutation: str) -> None:
    source = representative_source("fastapi")
    changes = {
        "return-missing": ("raise LookupError('not found')", "return {'error': 'not found'}"),
        "wrong-catch": ("except LookupError:", "except Exception:"),
        "nested": ("session.begin()", "session.begin_nested()"),
        "commit": ("session.flush()", "session.commit()"),
        "missing-id": ("if 'id' not in data['changed']:", "if False:"),
        "partial-null": (
            "changed.note = data['changed']['note']",
            "changed.note = data['changed']['note'] or 'default'",
        ),
        "scope-removed": ("with session.begin():", "if True:"),
        "side-effect": (
            "result = {'ok': True}",
            "print('effect')\n                result = {'ok': True}",
        ),
        "shadow": ("def relocate(data: dict):", "def LookupError(data: dict):"),
    }
    before, after = changes[mutation]
    assert before in source
    source = source.replace(before, after)
    (tmp_path / "app.py").write_text(source)
    (tmp_path / "models.py").write_text(models_source("fastapi"))
    captured = capture(
        tmp_path, configuration({"source_framework": "fastapi", "database_layer": "pgx"})
    )
    assert captured["gaps"]


def backend_scenarios() -> list[dict]:
    def parent(name: str) -> dict:
        return {"name": name, "count": 0, "enabled": True}

    def child(name: str, key: int) -> dict:
        return {"name": name, "parent_id": key, "enabled": True}

    def move(name: str, ident: int, old: int, child_name: str) -> dict:
        return {
            "created": parent(name),
            "changed": {"id": ident, "name": child_name, "enabled": True},
            "removed": {"id": old},
        }

    def revise(value: dict, checked: int = 3) -> dict:
        return {"changed": {"id": 1} | value, "checked": {"id": checked}}

    rows = [
        ("POST", "/parents", 201, parent("A")),
        ("POST", "/parents", 201, parent("B")),
        ("POST", "/widgets", 201, child("X", 1)),
        ("POST", "/widgets", 201, child("Y", 2)),
        ("GET", "/search?q=X", 200, None),
        ("POST", "/relocate", 400, {}),
        ("POST", "/relocate", 201, move("C", 1, 1, "X")),
        ("POST", "/relocate", 404, move("D", 999, 3, "X")),
        ("POST", "/relocate", 409, move("E", 1, 3, "Y")),
        ("POST", "/relocate", 404, move("F", 1, 999, "changed")),
        ("POST", "/revise", 201, revise({"note": "hello"})),
        ("POST", "/revise", 201, revise({"note": None})),
        ("POST", "/revise", 201, revise({})),
        ("POST", "/revise", 400, {"changed": {"note": None}, "checked": {"id": 3}}),
        ("POST", "/revise", 400, revise({"name": None})),
        ("POST", "/revise", 404, revise({"name": "changed"}, 999)),
        ("POST", "/revise", 409, revise({"parent_id": 999})),
        ("POST", "/erase", 404, {"removed": {"id": 2}, "parent": {"id": 999}}),
        (
            "POST",
            "/restore",
            404,
            {"removed": {"id": 2}, "created": parent("G"), "checked": {"id": 999}},
        ),
        ("POST", "/erase", 201, {"removed": {"id": 2}, "parent": {"id": 2}}),
        ("POST", "/relocate", 201, move("H", 1, 3, "X")),
        ("POST", "/erase", 201, {"removed": {"id": 1}, "parent": {"id": 8}}),
        ("POST", "/parents", 201, parent("I")),
        ("POST", "/widgets", 201, child("recovery", 9)),
        ("GET", "/search?q=recovery", 200, None),
    ]
    return [
        {
            "id": f"step{i}",
            "method": method,
            "path": path,
            "expected_status": status,
            **({"body": body} if body is not None else {}),
        }
        for i, (method, path, status, body) in enumerate(rows)
    ]


@pytest.mark.skipif(
    not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN") or os.getenv("SANKA_GO_TESTS") != "1",
    reason="requires isolated PostgreSQL fixtures and Go",
)
@pytest.mark.parametrize("framework", ["drf", "flask", "fastapi"])
@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_representative_backend_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, framework: str, target: str
) -> None:
    import psycopg
    from psycopg import sql
    from sanka_extension_python_to_golang.replay import replay

    (tmp_path / "sanka-verify.json").write_text(json.dumps({"scenarios": backend_scenarios()}))
    output = generate(
        tmp_path,
        framework,
        target,
        app_source=representative_source(framework),
        model_text=models_source(framework),
    )
    captured = capture(
        tmp_path,
        configuration(
            {"source_framework": framework, "target_framework": target, "database_layer": "pgx"}
        ),
    )
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    schemas = ["go_mixed_" + uuid.uuid4().hex for _ in range(2)]
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            for schema in schemas:
                admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            source, destination = [schema_dsn(dsn, schema) for schema in schemas]
            if framework != "drf":
                source = source.replace("postgresql://", "postgresql+psycopg://", 1)
            monkeypatch.setenv("SANKA_GO_SOURCE_TEST_DATABASE_URL", source)
            monkeypatch.setenv("SANKA_GO_TARGET_TEST_DATABASE_URL", destination)
            report = replay(tmp_path, output, captured, "verify")
            assert report["ok"], report["steps"]
            assert report["source"] == report["candidate"]
            all_rows = report["candidate"]
            for previous, current in pairwise(all_rows):
                if current["status"] in {400, 401, 403, 404, 409}:
                    assert current["tables"] == previous["tables"]
                if current["status"] in {400, 401, 403}:
                    assert current["sequences"] == previous["sequences"]
            rows = all_rows[::4]
            assert rows[10]["tables"]["widgets"][0]["note"] == "hello"
            assert rows[11]["tables"]["widgets"][0]["note"] is None
            assert rows[12]["tables"] == rows[11]["tables"]
            assert rows[21]["tables"] == {"parents": [], "widgets": []}
            assert rows[-1]["sequences"] == {"parents": ["9", True], "widgets": ["3", True]}
            assert rows[-1]["tables"]["widgets"][0]["parent_id"] == 9
            if framework == "fastapi" and target == "fiber":
                app = output / "app.go"
                text = app.read_text()
                index = next(
                    i for i, route in enumerate(captured["routes"]) if route["path"] == "/relocate"
                )
                start = text.index(f"func writeRow{index}(")
                end = text.index("\nfunc ", start + 1)
                helper = text[start:end]
                assert "tx.QueryRow(ctx," in helper
                app.write_text(
                    text[:start]
                    + helper.replace("tx.QueryRow(ctx,", "pool.QueryRow(ctx,", 1)
                    + text[end:]
                )
                changed = replay(tmp_path, output, captured, "verify")
                assert not changed["ok"]
                assert changed["candidate"][7 * 4]["tables"] != changed["source"][7 * 4]["tables"]
        finally:
            for schema in schemas:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )


def test_transaction_failure_invariant() -> None:
    from sanka_extension_python_to_golang.write_replay import integrity_failures_unchanged

    initial = {"tables": {"parents": [{"id": 1}]}, "sequences": {"parents": ["1", True]}}
    failed = initial | {"status": 404, "sequences": {"parents": ["2", True]}}
    assert integrity_failures_unchanged(initial, [failed], statuses=(404, 409))
    assert not integrity_failures_unchanged(
        initial, [failed | {"tables": {"parents": []}}], statuses=(404, 409)
    )


def test_literal_response_does_not_normalize_model_fields() -> None:
    from sanka_extension_python_to_golang.write_replay import normalize_bodies

    captured = {
        "models": [{"name": "Widget", "fields": [{"name": "id", "go_type": "int64"}]}],
        "routes": [
            {
                "path": "/bundle",
                "method": "POST",
                "write": {"model": "Widget", "literal_response": {"id": 5}},
            }
        ],
    }
    observed = [{"path": "/bundle", "method": "POST", "status": 201, "body": {"id": 5}}]
    normalize_bodies(observed, captured)
    assert observed[0]["body"] == {"id": 5}


@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_standalone_partial_transaction(tmp_path: Path, target: str) -> None:
    tree = ast.parse(representative_source("fastapi"))
    tree.body = [
        node for node in tree.body if not isinstance(node, ast.FunctionDef) or node.name == "revise"
    ]
    output = generate(
        tmp_path,
        "fastapi",
        target,
        app_source=ast.unparse(tree),
        model_text=models_source("fastapi"),
    )
    (output / "transaction_errors_test.go").write_text("""package backend
import ("errors"; "testing"; "github.com/jackc/pgx/v5"; "github.com/jackc/pgx/v5/pgconn")
func TestMissingUpdatedRecord(t *testing.T) {
    if err := transactionUpdateError(pgx.ErrNoRows); err == nil || errors.Is(err, pgx.ErrNoRows) {
        t.Fatal("lost update must fail, not become a source lookup 404")
    }
    conflict := &pgconn.PgError{Code: "23505"}
    if transactionUpdateError(conflict) != conflict { t.Fatal("integrity error lost") }
    if transactionUpdateError(nil) != nil { t.Fatal("success changed") }
}
""")
    if os.getenv("SANKA_GO_TESTS") == "1":
        run = subprocess.run(
            ["go", "test", "-mod=readonly", "-p=2", "./..."],
            cwd=output,
            env=os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"},
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert run.returncode == 0, run.stdout + run.stderr


def test_lookup_of_generated_primary_key(tmp_path: Path) -> None:
    tree = ast.parse(representative_source("fastapi"))
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "restore"
    )
    for node in ast.walk(function):
        if isinstance(node, ast.If) and ast.unparse(node.test).startswith("type(data['checked'])"):
            node.test = ast.parse(
                "type(data['checked']) is not dict or set(data['checked']) - set()", mode="eval"
            ).body
        if (
            isinstance(node, ast.Call)
            and ast.unparse(node.func) == "session.get"
            and ast.unparse(node.args[1]) == "data['checked']['id']"
        ):
            node.args[1] = ast.parse("created.id", mode="eval").body
    output = generate(
        tmp_path,
        "fastapi",
        "fiber",
        app_source=ast.unparse(tree),
        model_text=models_source("fastapi"),
    )
    captured = capture(
        tmp_path, configuration({"source_framework": "fastapi", "database_layer": "pgx"})
    )
    route = next(route for route in captured["routes"] if route["path"] == "/restore")
    assert route["write"]["transaction"][2]["lookup_reference"] == {"step": 1, "field": "id"}
    if os.getenv("SANKA_GO_TESTS") == "1":
        run = subprocess.run(
            ["go", "test", "-mod=readonly", "-p=2", "./..."],
            cwd=output,
            env=os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"},
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.parametrize("cascade", [False, True, "set-null"])
def test_sqlalchemy_identity_map_sensitive_transaction_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cascade: bool
) -> None:
    monkeypatch.setitem(
        STEPS,
        "cached",
        [
            ("before", "Widget", "lookup"),
            ("removed", "Parent", "delete"),
            ("after", "Widget", "lookup"),
        ],
    )
    (tmp_path / "app.py").write_text(representative_source("fastapi"))
    model_text = models_source("fastapi", cascade=cascade is True)
    if cascade == "set-null":
        model_text = model_text.replace(
            'ForeignKey("parents.id")', 'ForeignKey("parents.id", ondelete="SET NULL")'
        ).replace("parent_id: Mapped[int]", "parent_id: Mapped[int | None]")
    (tmp_path / "models.py").write_text(model_text)
    captured = capture(
        tmp_path, configuration({"source_framework": "fastapi", "database_layer": "pgx"})
    )
    assert any("cached lookups after deletion" in gap for gap in captured["gaps"])


def test_lookup_error_model_shadow_blocks(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        representative_source("fastapi")
        .replace("from models import Parent", "from models import LookupError")
        .replace("Parent", "LookupError")
    )
    (tmp_path / "models.py").write_text(models_source("fastapi").replace("Parent", "LookupError"))
    captured = capture(
        tmp_path, configuration({"source_framework": "fastapi", "database_layer": "pgx"})
    )
    assert captured["gaps"]

# SPDX-License-Identifier: Apache-2.0
"""Explicit integrity responses and ordered relational CRUD verification."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from sanka_extension_python_to_golang.capture import capture, configuration
from test_golang_schema import generate, model_source
from test_golang_writes import (
    combine_drf_methods,
    drf_write_source,
    fastapi_write_source,
    flask_write_source,
)


def backend_source(framework: str) -> str:
    source = {
        "drf": lambda: drf_write_source(combined=False),
        "flask": flask_write_source,
        "fastapi": fastapi_write_source,
    }[framework]()
    tree = ast.parse(source)
    handler = {
        "drf": 'return Response({"error": "integrity conflict"}, status=409)',
        "flask": 'return jsonify({"error": "integrity conflict"}), 409',
        "fastapi": 'raise HTTPException(status_code=409, detail="integrity conflict")',
    }[framework]
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            node.body = [
                ast.Try(
                    body=node.body,
                    handlers=[
                        ast.ExceptHandler(
                            type=ast.Name(id="IntegrityError", ctx=ast.Load()),
                            name=None,
                            body=ast.parse(handler).body,
                        )
                    ],
                    orelse=[],
                    finalbody=[],
                )
            ]
    source = ast.unparse(ast.fix_missing_locations(tree))
    if framework == "drf":
        source = combine_drf_methods(source)
    parent = ast.parse(source.replace("Widget", "Parent").replace("widget", "parent"))
    child = ast.parse(source.replace("count", "parent_id"))
    body = list(parent.body)
    for node in child.body:
        if (isinstance(node, ast.ImportFrom) and node.module == "models") or isinstance(
            node, ast.FunctionDef
        ):
            body.append(node)
        elif isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "urlpatterns":
            original = next(
                item
                for item in body
                if isinstance(item, ast.Assign) and ast.unparse(item.targets[0]) == "urlpatterns"
            )
            assert isinstance(original.value, ast.List) and isinstance(node.value, ast.List)
            original.value.elts.extend(node.value.elts)
            body.remove(original)
            body.append(original)
    module = "django.db" if framework == "drf" else "sqlalchemy.exc"
    body.insert(0, ast.parse(f"from {module} import IntegrityError").body[0])
    # Model imports must precede either set of functions.
    body = [n for n in body if isinstance(n, ast.ImportFrom)] + [
        n for n in body if not isinstance(n, ast.ImportFrom)
    ]
    return ast.unparse(ast.Module(body=body, type_ignores=[])) + "\n"


def models_source(framework: str, *, cascade: bool = False) -> str:
    original = model_source(framework)
    parent = original.replace("Widget", "Parent").replace("widgets", "parents")
    if framework == "drf":
        parent = parent.replace("BigAutoField", "AutoField")
        child = original[original.index("class Widget") :].replace(
            "count = models.IntegerField()",
            "parent = models.ForeignKey(Parent, on_delete=models.DO_NOTHING)",
        )
    else:
        parent = parent.replace("import BigInteger,", "import ForeignKey, BigInteger,")
        parent = parent.replace(
            "mapped_column(BigInteger, primary_key=True)",
            "mapped_column(Integer, primary_key=True)",
        )
        foreign = 'ForeignKey("parents.id"' + (', ondelete="CASCADE"' if cascade else "") + ")"
        child = original[original.index("class Widget") :].replace(
            "count: Mapped[int] = mapped_column(Integer)",
            f"parent_id: Mapped[int] = mapped_column(Integer, {foreign})",
        )
    return parent + child


@pytest.mark.parametrize("framework", ["drf", "flask", "fastapi"])
@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_relational_crud_capture(tmp_path: Path, framework: str, target: str) -> None:
    output = generate(
        tmp_path,
        framework,
        target,
        app_source=backend_source(framework),
        model_text=models_source(framework),
    )
    captured = capture(
        tmp_path,
        configuration(
            {"source_framework": framework, "target_framework": target, "database_layer": "pgx"}
        ),
    )
    assert len(captured["routes"]) == 8
    assert all(route["write"]["integrity_conflict"] for route in captured["routes"])
    assert "isIntegrityConflict(err)" in (output / "app.go").read_text()


def scenarios(*, cascade: bool = False) -> list[dict]:
    parent = {"name": "parent", "count": 0, "enabled": True}
    child = {"name": "child", "parent_id": 1, "enabled": True}
    steps = [
        ("POST", "/parents", 201, parent),
        ("POST", "/parents", 201, parent | {"name": "other"}),
        ("POST", "/widgets", 201, child),
        ("POST", "/widgets", 409, child),
        ("POST", "/widgets", 409, child | {"name": "orphan", "parent_id": 999}),
        ("PATCH", "/widgets/1", 409, {"parent_id": 999}),
        ("PUT", "/widgets/1", 409, child | {"name": "replaced", "parent_id": 999}),
        ("PATCH", "/widgets/1", 200, {"parent_id": 2}),
        ("PATCH", "/widgets/1", 200, {}),
        ("DELETE", "/parents/2", 204 if cascade else 409, None),
        ("DELETE", "/parents/1", 204, None),
        ("DELETE", "/widgets/1", 404 if cascade else 204, None),
        ("DELETE", "/parents/2", 404 if cascade else 204, None),
        ("POST", "/parents", 201, parent | {"name": "recreated"}),
        ("POST", "/widgets", 201, child | {"parent_id": 3}),
        ("PUT", "/widgets/4", 200, child | {"parent_id": 3, "note": "updated"}),
        ("PATCH", "/widgets/4", 200, {"note": None}),
    ]
    return [
        {
            "id": f"step{index}",
            "method": method,
            "path": path,
            "expected_status": status,
            **({"body": body} if body is not None else {}),
        }
        for index, (method, path, status, body) in enumerate(steps)
    ]


@pytest.mark.parametrize(
    "mutation", ["wrong-import", "wrong-type", "wrong-status", "finally", "inner"]
)
def test_integrity_handler_must_be_exact(tmp_path: Path, mutation: str) -> None:
    text = backend_source("fastapi")
    if mutation == "wrong-import":
        text = text.replace("from sqlalchemy.exc import", "from builtins import")
    elif mutation == "wrong-type":
        text = text.replace("except IntegrityError:", "except Exception:")
    elif mutation == "wrong-status":
        text = text.replace("status_code=409", "status_code=200")
    elif mutation == "finally":
        text = text.replace(
            "detail='integrity conflict')",
            "detail='integrity conflict')\n    finally:\n        pass",
        )
    else:
        text = text.replace("    try:\n", "    if True:\n        pass\n    try:\n")
    (tmp_path / "app.py").write_text(text)
    (tmp_path / "models.py").write_text(models_source("fastapi"))
    result = capture(
        tmp_path, configuration({"source_framework": "fastapi", "database_layer": "pgx"})
    )
    assert result["gaps"]


def test_relational_scenarios_are_explicit(tmp_path: Path) -> None:
    from sanka_extension_python_to_golang.write_replay import scenarios_for

    generate(
        tmp_path,
        "fastapi",
        "fiber",
        app_source=backend_source("fastapi"),
        model_text=models_source("fastapi"),
    )
    captured = capture(
        tmp_path, configuration({"source_framework": "fastapi", "database_layer": "pgx"})
    )
    with pytest.raises(ValueError, match="explicit ordered"):
        scenarios_for(tmp_path, captured)
    document = {"schema": "sanka.http-scenarios/v1", "scenarios": scenarios()}
    (tmp_path / "sanka-verify.json").write_text(json.dumps(document))
    assert scenarios_for(tmp_path, captured) == [case | {"headers": {}} for case in scenarios()]


@pytest.mark.skipif(os.getenv("SANKA_GO_TESTS") != "1", reason="requires qualified Go toolchain")
@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_integrity_go_contract(tmp_path: Path, target: str) -> None:
    output = generate(
        tmp_path,
        "fastapi",
        target,
        app_source=backend_source("fastapi"),
        model_text=models_source("fastapi"),
    )
    (output / "integrity_test.go").write_text("""package backend
import ("testing"; "errors"; "fmt"; "github.com/jackc/pgx/v5/pgconn")
func TestIntegrityClassification(t *testing.T) {
    for _, code := range []string{"23503", "23505", "23502", "23514", "23001"} {
        err := fmt.Errorf("transaction: %w", &pgconn.PgError{Code:code})
        if !isIntegrityConflict(err) { t.Fatal(code) }
    }
    for _, err := range []error{nil, errors.New("23503"),
        &pgconn.PgError{Code:"40001"}, &pgconn.PgError{Code:"23"}} {
        if isIntegrityConflict(err) { t.Fatal("unrelated error classified as integrity conflict") }
    }
}
""")
    result = subprocess.run(
        ["go", "test", "-mod=readonly", "-p=2", "./..."],
        cwd=output,
        env=os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"},
        text=True,
        capture_output=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(
    not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN") or os.getenv("SANKA_GO_TESTS") != "1",
    reason="requires isolated PostgreSQL fixtures and Go",
)
@pytest.mark.parametrize(
    "framework,target,cascade",
    [
        (framework, target, False)
        for framework in ("drf", "flask", "fastapi")
        for target in ("fiber", "chi", "mux", "gin")
    ]
    + [(framework, "fiber", True) for framework in ("flask", "fastapi")],
)
def test_relational_write_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, framework: str, target: str, cascade: bool
) -> None:
    import psycopg
    from psycopg import sql
    from sanka_extension_python_to_golang.replay import replay
    from test_golang_schema import schema_dsn

    (tmp_path / "sanka-verify.json").write_text(
        json.dumps({"scenarios": scenarios(cascade=cascade)})
    )
    output = generate(
        tmp_path,
        framework,
        target,
        app_source=backend_source(framework),
        model_text=models_source(framework, cascade=cascade),
    )
    captured = capture(
        tmp_path,
        configuration(
            {"source_framework": framework, "target_framework": target, "database_layer": "pgx"}
        ),
    )
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    schemas = ["go_relwrites_" + uuid.uuid4().hex for _ in range(2)]
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
            assert report["candidate"] == report["source"]
            rows = report["candidate"]
            assert rows[3]["tables"] == rows[2]["tables"]
            assert rows[4]["tables"] == rows[3]["tables"]
            assert rows[4]["sequences"]["widgets"] == ["3", True]
            assert rows[5]["tables"] == rows[6]["tables"] == rows[4]["tables"]
            assert rows[9]["tables"]["widgets"] == ([] if cascade else rows[8]["tables"]["widgets"])
            assert rows[-1]["sequences"]["widgets"] == ["4", True]
            if framework == "fastapi" and target == "fiber" and not cascade:
                # A response-preserving mutation of a different table must fail parity.
                app = output / "app.go"
                text = app.read_text()
                needle = "return saved, err"
                assert needle in text
                app.write_text(
                    text.replace(
                        needle,
                        "if err == nil { _, err = pool.Exec(ctx, "
                        "\"UPDATE parents SET note='tampered'\") }; return saved, err",
                    )
                )
                changed = replay(tmp_path, output, captured, "verify")
                assert not changed["ok"]
                assert any(
                    a["tables"] != b["tables"]
                    for a, b in zip(changed["candidate"], changed["source"], strict=True)
                )
        finally:
            for schema in schemas:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )


def test_integrity_failure_must_not_change_any_table() -> None:
    from sanka_extension_python_to_golang.write_replay import integrity_failures_unchanged

    initial = {"tables": {"parents": [], "widgets": []}, "sequences": {"widgets": ["1", False]}}
    failed = initial | {"status": 409, "sequences": {"widgets": ["1", True]}}
    assert integrity_failures_unchanged(initial, [failed])
    assert not integrity_failures_unchanged(
        initial,
        [
            failed
            | {
                "tables": {"parents": [{"id": 1}], "widgets": []},
            }
        ],
    )

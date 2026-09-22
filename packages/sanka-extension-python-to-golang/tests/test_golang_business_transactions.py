# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""Explicit multi-record transactions, generated keys and whole-request rollback."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import uuid
from pathlib import Path
from textwrap import indent

import pytest
from sanka_extension_python_to_golang.capture import capture, configuration
from test_golang_relational_writes import backend_source
from test_golang_schema import generate, schema_dsn
from test_golang_security import secured_source


def models_source(framework: str, deferred: bool = False) -> str:
    if framework == "drf":
        return """from django.db import models
class Parent(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=40, unique=True)
    class Meta:
        app_label = "catalog"
        db_table = "parents"
class Child(models.Model):
    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=40, unique=True)
    parent = models.ForeignKey(Parent, on_delete=models.DO_NOTHING)
    class Meta:
        app_label = "catalog"
        db_table = "children"
"""
    options = ', deferrable=True, initially="DEFERRED"' if deferred else ""
    return f'''from sqlalchemy import BigInteger, Integer, String, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
class Base(DeclarativeBase):
    pass
class Parent(Base):
    __tablename__ = "parents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(40), unique=True)
class Child(Base):
    __tablename__ = "children"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(40), unique=True)
    parent_id: Mapped[int] = mapped_column(Integer, ForeignKey("parents.id"{options}))
'''


def transaction_source(framework: str, linked: bool = True) -> str:
    # Reuse only the existing framework registration/import fixtures.
    base = ast.parse(backend_source(framework))
    header = []
    for node in base.body:
        if isinstance(node, ast.ImportFrom):
            if node.module == "models":
                continue
            header.append(node)
        elif isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) != "urlpatterns":
            header.append(node)
    header.append(ast.parse("from models import Parent, Child").body[0])
    if framework == "drf":
        header.append(ast.parse("from django.db import transaction").body[0])
    response = {
        "drf": "return Response(result, status=201)",
        "flask": "return jsonify(result), 201",
        "fastapi": "return result",
    }[framework]
    invalid = {
        "drf": 'return Response({"error": "invalid request body"}, status=400)',
        "flask": 'return jsonify({"error": "invalid request body"}), 400',
        "fastapi": 'raise HTTPException(status_code=400, detail="invalid request body")',
    }[framework]
    conflict = invalid.replace("invalid request body", "integrity conflict").replace("400", "409")
    data = "request.data" if framework == "drf" else "data"
    validation = (
        f"if type({data}) is not dict or set({data}) != {{'parent', 'child'}}:\n    {invalid}\n"
    )
    for key in ["parent", "child"]:
        value = f"{data}[{key!r}]"
        fields = "{'name', 'parent_id'}" if key == "child" and not linked else "{'name'}"
        condition = f"type({value}) is not dict or set({value}) - {fields} or 'name' not in {value} or type({value}['name']) is not str"
        if key == "child" and not linked:
            condition += f" or 'parent_id' not in {value} or (type({value}['parent_id']) is not int or not -2147483648 <= {value}['parent_id'] <= 2147483647)"
        validation += f"if {condition}:\n    {invalid}\n"
    parent_key = "parent.id" if linked else f"{data}['child']['parent_id']"
    parent_args = f"name={data}['parent']['name']"
    child_args = f"name={data}['child']['name'], parent_id={parent_key}"
    snapshot = "result = {'id': child.id, 'name': child.name, 'parent_id': child.parent_id}"
    if framework == "drf":
        scope = "with transaction.atomic():\n" + indent(
            f"parent = Parent.objects.create({parent_args})\nchild = Child.objects.create({child_args})\n{snapshot}\n",
            "    ",
        )
        decorators = """@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@renderer_classes([JSONRenderer])
"""
        signature = "request"
    else:
        scope = "with Session(engine) as session:\n    with session.begin():\n" + indent(
            f"parent = Parent({parent_args})\nsession.add(parent)\nsession.flush()\nchild = Child({child_args})\nsession.add(child)\nsession.flush()\n{snapshot}\n",
            "        ",
        )
        decorators = (
            '@app.post("/bundles"' + (", status_code=201" if framework == "fastapi" else "") + ")\n"
        )
        signature = "data: dict" if framework == "fastapi" else ""
    prefix = "data = request.get_json()\n" if framework == "flask" else ""
    body = (
        "try:\n"
        + indent(prefix + validation + scope + response + "\n", "    ")
        + f"except IntegrityError:\n    {conflict}\n"
    )
    text = (
        ast.unparse(ast.Module(body=header, type_ignores=[]))
        + "\n"
        + decorators
        + f"def create_bundle({signature}):\n"
        + indent(body, "    ")
    )
    if framework == "drf":
        text += "urlpatterns = [path('bundles', create_bundle)]\n"
    return text


@pytest.mark.parametrize("framework", ["drf", "flask", "fastapi"])
@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_business_transaction_capture(tmp_path: Path, framework: str, target: str) -> None:
    output = generate(
        tmp_path,
        framework,
        target,
        app_source=secured_source(framework, transaction_source(framework)),
        model_text=models_source(framework),
    )
    captured = capture(
        tmp_path,
        configuration(
            {"source_framework": framework, "target_framework": target, "database_layer": "pgx"}
        ),
    )
    write = captured["routes"][0]["write"]
    assert len(write["transaction"]) == 2
    assert write["transaction"][1]["references"] == {"parent_id": {"step": 0, "field": "id"}}
    text = (output / "app.go").read_text()
    assert text.count("pgx.BeginFunc(") == 1
    assert text.count("tx.QueryRow(") == 2
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


@pytest.mark.parametrize(
    "mutation",
    [
        "no-scope",
        "nested",
        "missing-flush",
        "side-effect",
        "bad-reference",
        "inner-catch",
        "missing-validation",
        "shadow",
    ],
)
def test_unqualified_transaction_blocks(tmp_path: Path, mutation: str) -> None:
    text = transaction_source("fastapi")
    if mutation == "no-scope":
        text = text.replace("session.begin()", "session.begin_nested()")
    elif mutation == "nested":
        text = text.replace("session.begin()", "session.begin(), session.begin_nested()")
    elif mutation == "missing-flush":
        text = text.replace("session.flush()", "pass", 1)
    elif mutation == "side-effect":
        text = text.replace("session.flush()", "session.commit()", 1)
    elif mutation == "bad-reference":
        text = text.replace("parent_id=parent.id", "parent_id=child.id")
    elif mutation == "inner-catch":
        text = text.replace("except IntegrityError:", "except Exception:")
    elif mutation == "missing-validation":
        text = text.replace("type(data['parent']['name']) is not str", "False")
    else:
        text = text.replace("parent", "Session")
    (tmp_path / "app.py").write_text(text)
    (tmp_path / "models.py").write_text(models_source("fastapi"))
    captured = capture(
        tmp_path, configuration({"source_framework": "fastapi", "database_layer": "pgx"})
    )
    assert captured["gaps"]


@pytest.mark.parametrize("framework", ["drf", "flask", "fastapi"])
def test_transaction_with_input_foreign_key(tmp_path: Path, framework: str) -> None:
    generate(
        tmp_path,
        framework,
        "fiber",
        app_source=transaction_source(framework, False),
        model_text=models_source(framework, True),
    )


def test_transaction_requires_ordered_scenarios(tmp_path: Path) -> None:
    from sanka_extension_python_to_golang.write_replay import scenarios_for

    generate(
        tmp_path,
        "fastapi",
        "fiber",
        app_source=transaction_source("fastapi"),
        model_text=models_source("fastapi"),
    )
    captured = capture(
        tmp_path, configuration({"source_framework": "fastapi", "database_layer": "pgx"})
    )
    with pytest.raises(ValueError, match="transactions require explicit ordered"):
        scenarios_for(tmp_path, captured)


def transaction_scenarios(linked: bool) -> list[dict]:
    def body(parent: str, child: str, key: int) -> dict:
        return {
            "parent": {"name": parent},
            "child": {"name": child, **({} if linked else {"parent_id": key})},
        }

    cases = [
        (400, {}),
        (400, {"parent": {"name": "p"}, "child": {"name": True}}),
        (400, body("p", "c", 1) | {"extra": 1}),
        (400, body("p", "c", 1) | {"parent": {"name": "p", "id": 99}}),
        (201, body("p1", "c1", 1)),
        (409, body("p2", "c1" if linked else "c2", 999)),
        (201, body("p3", "c3", 3)),
    ]
    return [
        {
            "id": f"step{i}",
            "method": "POST",
            "path": "/bundles",
            "body": value,
            "expected_status": status,
        }
        for i, (status, value) in enumerate(cases)
    ]


@pytest.mark.skipif(
    not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN") or os.getenv("SANKA_GO_TESTS") != "1",
    reason="requires isolated PostgreSQL fixtures and Go",
)
@pytest.mark.parametrize(
    "framework,target,linked",
    [
        (framework, target, True)
        for framework in ("drf", "flask", "fastapi")
        for target in ("fiber", "chi", "mux", "gin")
    ]
    + [(framework, "fiber", False) for framework in ("drf", "flask", "fastapi")],
)
def test_business_transaction_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, framework: str, target: str, linked: bool
) -> None:
    import psycopg
    from psycopg import sql
    from sanka_extension_python_to_golang.replay import replay

    (tmp_path / "sanka-verify.json").write_text(
        json.dumps({"scenarios": transaction_scenarios(linked)})
    )
    output = generate(
        tmp_path,
        framework,
        target,
        app_source=secured_source(framework, transaction_source(framework, linked)),
        model_text=models_source(framework, not linked),
    )
    captured = capture(
        tmp_path,
        configuration(
            {"source_framework": framework, "target_framework": target, "database_layer": "pgx"}
        ),
    )
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    schemas = ["go_business_" + uuid.uuid4().hex for _ in range(2)]
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
            rows = report["candidate"][::4]
            for index in range(0, len(report["candidate"]), 4):
                writer = report["candidate"][index]
                for denied in report["candidate"][index + 1 : index + 4]:
                    assert denied["status"] in {401, 403}
                    assert denied["tables"] == writer["tables"]
                    assert denied["sequences"] == writer["sequences"]
            assert rows[5]["tables"] == rows[4]["tables"]
            assert rows[5]["sequences"] == {"parents": ["2", True], "children": ["2", True]}
            assert rows[6]["sequences"] == {"parents": ["3", True], "children": ["3", True]}
            assert [row["id"] for row in rows[6]["tables"]["parents"]] == [1, 3]
            assert [row["parent_id"] for row in rows[6]["tables"]["children"]] == [1, 3]
            if framework == "fastapi" and target == "fiber" and linked:
                # Deliberately let the parent escape the transaction. HTTP status
                # alone still agrees; all-table parity must reject partial writes.
                app = output / "app.go"
                text = app.read_text()
                assert "tx.QueryRow(ctx," in text
                app.write_text(text.replace("tx.QueryRow(ctx,", "pool.QueryRow(ctx,", 1))
                changed = replay(tmp_path, output, captured, "verify")
                assert not changed["ok"]
                assert changed["candidate"][20]["tables"] != changed["source"][20]["tables"]
        finally:
            for schema in schemas:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )


def test_transaction_without_input_fields_is_rejected(tmp_path: Path) -> None:
    tree = ast.parse(transaction_source("fastapi"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef))
    for node in ast.walk(function):
        if isinstance(node, ast.If) and "type(data['child'])" in ast.unparse(node.test):
            node.test = ast.parse(
                "type(data['child']) is not dict or set(data['child']) - {}", mode="eval"
            ).body
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Child"
        ):
            node.keywords = [kw for kw in node.keywords if kw.arg != "name"]
        if isinstance(node, ast.Dict) and any(
            isinstance(value, ast.Attribute) and value.attr == "parent_id" for value in node.values
        ):
            pairs = [
                (key, value)
                for key, value in zip(node.keys, node.values, strict=True)
                if not isinstance(key, ast.Constant) or key.value != "name"
            ]
            node.keys = [key for key, _ in pairs]
            node.values = [value for _, value in pairs]
    model_tree = ast.parse(models_source("fastapi"))
    child = next(
        node for node in model_tree.body if isinstance(node, ast.ClassDef) and node.name == "Child"
    )
    child.body = [
        node
        for node in child.body
        if not (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "name"
        )
    ]
    (tmp_path / "app.py").write_text(ast.unparse(tree))
    (tmp_path / "models.py").write_text(ast.unparse(model_tree))
    captured = capture(
        tmp_path, configuration({"source_framework": "fastapi", "database_layer": "pgx"})
    )
    assert captured["gaps"]

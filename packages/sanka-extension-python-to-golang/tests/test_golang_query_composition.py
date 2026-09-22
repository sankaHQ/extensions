# SPDX-License-Identifier: Apache-2.0
"""Composed predicates and pagination preserve each source query contract."""

import ast
import os
import subprocess
from pathlib import Path

import pytest
from sanka_extension_python_to_golang.capture import SOURCES, TARGETS
from test_golang_filters import filter_source
from test_golang_schema import generate
from test_golang_validation import captured_source


def composed_source(framework: str, paged: bool = True) -> str:
    text = filter_source(framework)
    if framework == "drf":
        text = text.replace(
            """.filter(name=request.query_params.get("q", 'first'))""",
            """.filter(name=request.query_params.get("q", 'first'), """
            """note=request.query_params.get("note", 'memo'))""",
        )
    elif framework == "flask":
        text = text.replace(
            """.where(Widget.name == request.args.get("q", 'first'))""",
            """.where(Widget.name == request.args.get("q", 'first'), """
            """Widget.note == request.args.get("note", 'memo'))""",
        )
    else:
        text = text.replace("q: str = 'first'", "q: str = 'first', note: str = 'memo'")
        text = text.replace(
            ".where(Widget.name == q)", ".where(Widget.name == q, Widget.note == note)"
        )
    if not paged:
        return text
    tree = ast.parse(text)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef))
    prefix = []
    if framework == "fastapi":
        for name, default in [("limit", "2"), ("offset", "0")]:
            node.args.args.append(ast.arg(arg=name, annotation=ast.Name(id="str", ctx=ast.Load())))
            node.args.defaults.append(ast.Constant(value=default))
        tree.body.insert(0, ast.parse("from fastapi import HTTPException").body[0])
    else:
        accessor = "request.query_params" if framework == "drf" else "request.args"
        prefix = ast.parse(
            f'limit = {accessor}.get("limit", "2")\noffset = {accessor}.get("offset", "0")'
        ).body
    error = {
        "drf": 'return Response({"error": "invalid pagination"}, status=400)',
        "flask": 'return jsonify({"error": "invalid pagination"}), 400',
        "fastapi": 'raise HTTPException(status_code=400, detail="invalid pagination")',
    }[framework]
    prefix += ast.parse(
        "if not (limit.isascii() and limit.isdecimal() and len(limit) <= 4 "
        "and 1 <= int(limit) <= 1000 and offset.isascii() and offset.isdecimal() "
        "and len(offset) <= 10 and 0 <= int(offset) <= 2147483647):\n    " + error
    ).body
    if framework == "drf":
        node.body = prefix + node.body
    else:
        node.body[0].body = prefix + node.body[0].body
    text = ast.unparse(ast.fix_missing_locations(tree))
    return text.replace("[:2]", "[int(offset):int(offset) + int(limit)]").replace(
        ".limit(2)", ".limit(int(limit)).offset(int(offset))"
    )


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("paged", [False, True])
def test_composed_query(tmp_path: Path, framework: str, target: str, paged: bool) -> None:
    text = composed_source(framework, paged)
    captured = captured_source(tmp_path, framework, target, text)
    assert captured["gaps"] == []
    read = captured["routes"][0]["read"]
    assert read["filters"] == [
        {"field": "name", "parameter": "q", "default": "first"},
        {"field": "note", "parameter": "note", "default": "memo"},
    ]
    assert ("pagination" in read) == paged
    output = generate(tmp_path, framework, target, app_source=text)
    go = (output / "app.go").read_text()
    assert 'AND \\"note\\" = $' in go
    assert captured_source(tmp_path, framework, target, text) == captured


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize(
    "before,after",
    [
        ("limit.isascii() and ", ""),
        ("len(limit) <= 4", "len(limit) <= 5"),
        ("int(offset) + int(limit)", "int(limit)"),
        ("Widget.note ==", "Widget.count =="),
        ("note=request.query_params", "count=request.query_params"),
    ],
)
def test_changed_query_blocks(tmp_path: Path, framework: str, before: str, after: str) -> None:
    text = composed_source(framework)
    if before not in text:
        pytest.skip("different source syntax")
    assert captured_source(tmp_path, framework, text=text.replace(before, after))["gaps"]


def assert_error_parity(
    root: Path, output: Path, framework: str, target: str, paths: list[str]
) -> None:
    import json
    import sys

    from sanka_extension_python_to_golang.replay import SOURCE_PROBE, _probe

    database_url = "sqlite://"
    if "create_async_engine" in (root / "app.py").read_text():
        database_url = "postgresql+psycopg://unused@127.0.0.1/unused"
    observed = root / ".sanka/source-errors.json"
    source = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            SOURCE_PROBE,
            framework,
            str(root / "app.py"),
            json.dumps(paths),
            str(observed),
            str(root / "models.py"),
            "0",
        ],
        env=os.environ | {"DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert source.returncode == 0, source.stdout + source.stderr
    probe = _probe(target, paths).replace("app := NewApp()", "app := NewApp(&pgxpool.Pool{})")
    probe = probe.replace("import (", 'import ("github.com/jackc/pgx/v5/pgxpool"; ', 1)
    (output / "errors_test.go").write_text(probe)
    result = subprocess.run(
        ["go", "test", "-mod=readonly", "-p=2", "./..."],
        cwd=output,
        env=os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    expected = json.loads(observed.read_text())
    assert all(row["status"] == 400 for row in expected)
    assert json.loads((output / "sanka-observed.json").read_text()) == expected


@pytest.mark.skipif(os.getenv("SANKA_GO_TESTS") != "1", reason="requires Go toolchain")
@pytest.mark.parametrize("framework", [*SOURCES, "fastapi-async"])
@pytest.mark.parametrize("target", TARGETS)
def test_generated_query_compiles(tmp_path: Path, framework: str, target: str) -> None:
    source = (
        conventional_backend(framework)
        if framework.endswith("-async")
        else composed_source(framework)
    )
    framework = framework.removesuffix("-async")
    output = generate(tmp_path, framework, target, app_source=source)
    (output / "composed_test.go").write_text(
        """package backend
import "testing"
func TestPageParsing(t *testing.T) {
    for _, raw := range []string{"", "0", "1001", "-1", "+1", "1.0", "00001"} {
        if _, err := pageValue(raw, 4, 1, 1000); err == nil { t.Fatalf("accepted %q", raw) }
    }
    if got, err := pageValue("0001", 4, 1, 1000); got != 1 || err != nil { t.Fatal(got, err) }
    if got := queryValue("q=first&q=last", "q", "fallback"); got != EXPECTED { t.Fatal(got) }
}
""".replace("EXPECTED", '"first"' if framework == "flask" else '"last"')
    )
    queries = [
        "limit=0",
        "offset=-1",
        "limit=%D9%A1",
        "limit=%FF",
        "limit=00001",
        "offset=2147483648",
    ]
    queries.append("limit=bad&limit=1" if framework == "flask" else "limit=1&limit=bad")
    assert_error_parity(tmp_path, output, framework, target, ["/health?" + q for q in queries])


@pytest.mark.parametrize("style", ["direct", "function", "class"])
@pytest.mark.parametrize("annotated", [False, True])
def test_keyword_session_with_query_defaults(tmp_path: Path, style: str, annotated: bool) -> None:
    from test_golang_async_persistence import injected_source

    text = injected_source(style, composed_source("fastapi"))
    text = text.replace(
        "session: AsyncSession=Depends(get_session)",
        "*, session: AsyncSession=Depends(get_session)",
    )
    if annotated:
        text = "from typing import Annotated\n" + text.replace(
            "session: AsyncSession=Depends(get_session)",
            "session: Annotated[AsyncSession, Depends(get_session)]",
        )
    compile(text, "fixture.py", "exec")
    captured = captured_source(tmp_path, "fastapi", text=text)
    assert captured["gaps"] == []
    assert (
        captured["routes"]
        == captured_source(tmp_path, "fastapi", text=composed_source("fastapi"))["routes"]
    )


def uuid_relationship_source(framework: str) -> tuple[str, str]:
    from sanka_extension_python_to_golang.values import SOURCE_VALIDATORS, uuid_lookup_prefix
    from test_golang_relations import related_read_source, relational_source

    model = relational_source(framework)
    if framework == "drf":
        model = model.replace(
            "models.BigAutoField(primary_key=True)", "models.UUIDField(primary_key=True)", 1
        )
    else:
        model = model.replace(
            "import BigInteger, ForeignKey", "import BigInteger, ForeignKey, Uuid"
        )
        model = model.replace(
            "mapped_column(BigInteger, primary_key=True)",
            "mapped_column(Uuid, primary_key=True)",
            1,
        )
        model = model.replace(
            "mapped_column(BigInteger, ForeignKey", "mapped_column(Uuid, ForeignKey"
        )
        model = "from uuid import UUID\n" + model.replace(
            "Mapped[int] = mapped_column(Uuid", "Mapped[UUID] = mapped_column(Uuid"
        )
    text = related_read_source(framework)
    if framework == "drf":
        text = text.replace(
            "list(Child.objects",
            '[{"id": row["id"], "parent_id": str(row["parent_id"])} for row in Child.objects',
        ).replace("[:2]))", "[:2]])")
    else:
        text = text.replace("dict(row)", '{"id": row["id"], "parent_id": str(row["parent_id"])}')
    text = text.replace(
        "<int:parent_id>", "<str:parent_id>" if framework == "drf" else "<parent_id>"
    ).replace("parent_id: int", "parent_id: str")
    tree = ast.parse(text)
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef))
    function.body[:0] = ast.parse(
        uuid_lookup_prefix({"name": "parent_id", "go_type": "UUIDValue"}, framework)
    ).body
    validator = next(
        n
        for n in ast.parse(SOURCE_VALIDATORS).body
        if isinstance(n, ast.FunctionDef) and n.name == "valid_uuid"
    )
    tree.body[:0] = [*ast.parse("from uuid import UUID\nfrom re import fullmatch").body, validator]
    text = ast.unparse(ast.fix_missing_locations(tree))
    return text, model


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
def test_uuid_relationship_query(tmp_path: Path, framework: str, target: str) -> None:
    text, model = uuid_relationship_source(framework)
    output = generate(tmp_path, framework, target, model_text=model, app_source=text)
    assert "lookup UUIDValue" in (output / "app.go").read_text()
    if os.getenv("SANKA_GO_TESTS") == "1":
        assert_error_parity(
            tmp_path,
            output,
            framework,
            target,
            ["/widgets/invalid", "/widgets/12345678-1234-5678-9ABC-123456789abc"],
        )


def conventional_backend(framework: str) -> str:
    from test_golang_async_persistence import injected_source
    from test_golang_drf_validation import drf_field_serializer_source
    from test_golang_validation import native_fastapi_schema_source, schema_source

    asynchronous = framework == "fastapi-async"
    framework = "fastapi" if asynchronous else framework
    source = (
        drf_field_serializer_source()
        if framework == "drf"
        else native_fastapi_schema_source()
        if framework == "fastapi"
        else schema_source("flask")
    )
    if framework == "fastapi":
        source = source.replace("ge=-2147483648", "ge=0").replace("le=2147483647", "le=100")
    tree = ast.parse(source)
    query = next(
        n for n in ast.parse(composed_source(framework)).body if isinstance(n, ast.FunctionDef)
    )
    if framework == "drf":
        urls = next(
            n
            for n in tree.body
            if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "urlpatterns"
        )
        urls.value.elts.append(ast.parse("path('health', health)", mode="eval").body)
        tree.body.insert(tree.body.index(urls), query)
    else:
        tree.body.insert(0, ast.parse("from sqlalchemy import select").body[0])
        tree.body.append(query)
    text = ast.unparse(ast.fix_missing_locations(tree))
    if asynchronous:
        text = injected_source("class", text).replace(
            "session: AsyncSession=Depends(get_session)",
            "*, session: AsyncSession=Depends(get_session)",
        )
        text = "from typing import Annotated\n" + text.replace(
            "session: AsyncSession=Depends(get_session)",
            "session: Annotated[AsyncSession, Depends(get_session)]",
        )
    return text


@pytest.mark.parametrize("framework", [*SOURCES, "fastapi-async"])
def test_conventional_backend_capture(tmp_path: Path, framework: str) -> None:
    assert (
        captured_source(
            tmp_path, framework.removesuffix("-async"), text=conventional_backend(framework)
        )["gaps"]
        == []
    )


@pytest.mark.skipif(
    os.getenv("SANKA_GO_TESTS") != "1" or not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN"),
    reason="requires isolated PostgreSQL fixtures and Go",
)
@pytest.mark.parametrize("framework", [*SOURCES, "fastapi-async"])
@pytest.mark.parametrize("target", TARGETS)
def test_conventional_backend_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, framework: str, target: str
) -> None:
    import dataclasses
    import json
    import uuid

    import psycopg
    from psycopg import sql
    from sanka_extension_python_to_golang.adapter import handle
    from test_golang_schema import schema_dsn
    from test_python_to_golang import request

    text = conventional_backend(framework)
    framework = framework.removesuffix("-async")
    scenarios = [
        {
            "id": f"create-{i}",
            "method": "POST",
            "path": "/widgets",
            "expected_status": 201,
            "body": {"name": name, "note": note, "count": i, "enabled": True},
        }
        for i, (name, note) in enumerate(
            [("first", "memo"), ("last", "memo"), ("日本語", "other")], 1
        )
    ]
    for i, (query, status) in enumerate(
        [
            ("", 200),
            ("q=last&note=memo", 200),
            ("q=first&note=other", 200),
            ("limit=1&offset=1", 200),
            ("q=first&q=last&note=memo", 200),
            ("limit=bad&limit=1", 400 if framework == "flask" else 200),
            ("limit=1&limit=bad", 200 if framework == "flask" else 400),
            ("limit=0", 400),
            ("offset=2147483648", 400),
            ("limit=%D9%A1", 400),
        ]
    ):
        scenarios.append(
            {
                "id": f"query-{i}",
                "method": "GET",
                "path": "/health" + ("?" + query if query else ""),
                "expected_status": status,
            }
        )
    scenarios += [
        {
            "id": "null-note",
            "method": "PATCH",
            "path": "/widgets/1",
            "body": {"note": None},
            "expected_status": 200,
        },
        {"id": "after-null", "method": "GET", "path": "/health", "expected_status": 200},
        {"id": "delete", "method": "DELETE", "path": "/widgets/2", "expected_status": 204},
        {"id": "after-delete", "method": "GET", "path": "/health?q=last", "expected_status": 200},
    ]
    (tmp_path / "sanka-verify.json").write_text(json.dumps({"scenarios": scenarios}))
    generate(tmp_path, framework, target, app_source=text)
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    schemas = ["composed_" + uuid.uuid4().hex for _ in range(2)]
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            for name in schemas:
                admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
            source, candidate = [schema_dsn(dsn, name) for name in schemas]
            if framework != "drf":
                source = source.replace("postgresql://", "postgresql+psycopg://", 1)
            monkeypatch.setenv("SANKA_GO_SOURCE_TEST_DATABASE_URL", source)
            monkeypatch.setenv("SANKA_GO_TARGET_TEST_DATABASE_URL", candidate)
            req = request(tmp_path, framework, target)
            result = handle(
                dataclasses.replace(
                    req,
                    command="verify",
                    configuration=req.configuration | {"database_layer": "pgx"},
                )
            )
            assert result.outcome == "success", result.error
            assert result.data["source"] == result.data["candidate"]
            observations = {o["id"]: o for o in result.data["candidate"]}
            assert observations["query-0"]["body"][0]["name"] == "first"
            assert observations["query-2"]["body"] == []
            assert observations["query-3"]["body"] == []
            assert observations["after-null"]["body"] == []
        finally:
            for name in schemas:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name))
                )


def test_duplicate_drf_predicates_block(tmp_path: Path) -> None:
    text = composed_source("drf", False).replace(
        "note=request.query_params", "name=request.query_params"
    )
    assert captured_source(tmp_path, "drf", text=text)["gaps"]


@pytest.mark.parametrize("mutation", ["extra", "default", "metadata", "provider"])
def test_keyword_dependency_semantics_block(tmp_path: Path, mutation: str) -> None:
    text = conventional_backend("fastapi-async")
    if mutation == "extra":
        text = text.replace("*, session:", "*, ignored: str = 'x', session:")
    elif mutation == "default":
        text = text.replace(
            "Annotated[AsyncSession, Depends(get_session)]",
            "Annotated[AsyncSession, Depends(get_session)] = None",
        )
    elif mutation == "metadata":
        text = text.replace(
            "Annotated[AsyncSession, Depends(get_session)]",
            "Annotated[AsyncSession, Depends(get_session), 'extra']",
        )
    else:
        text = text.replace("yield session", "await session.commit()\n        yield session")
    assert captured_source(tmp_path, "fastapi", text=text)["gaps"]


@pytest.mark.skipif(
    os.getenv("SANKA_GO_TESTS") != "1" or not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN"),
    reason="requires isolated PostgreSQL fixtures and Go",
)
@pytest.mark.parametrize("framework", SOURCES)
def test_uuid_relationship_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, framework: str
) -> None:
    import dataclasses
    import sys
    import uuid

    import psycopg
    from psycopg import sql
    from sanka_extension_python_to_golang.adapter import handle
    from test_golang_schema import SOURCE_DDL, schema_dsn
    from test_python_to_golang import request

    text, models = uuid_relationship_source(framework)
    output = generate(tmp_path, framework, "fiber", app_source=text, model_text=models)
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    schemas = ["uuid_read_" + uuid.uuid4().hex for _ in range(2)]
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            for name in schemas:
                admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
            source, candidate = [schema_dsn(dsn, name) for name in schemas]
            ddl = SOURCE_DDL.replace(
                "editor.create_model(module.Widget)",
                "editor.create_model(module.Parent)\n        editor.create_model(module.Child)",
            )
            subprocess.run(
                [sys.executable, "-I", "-c", ddl, framework, str(tmp_path / "models.py"), source],
                check=True,
                capture_output=True,
                timeout=30,
            )
            subprocess.run(
                ["go", "run", "-mod=readonly", "-p=2", "./cmd/migrate", "up"],
                cwd=output,
                env=os.environ
                | {
                    "GOTOOLCHAIN": "local",
                    "GOWORK": "off",
                    "GOMAXPROCS": "2",
                    "DATABASE_URL": candidate,
                },
                check=True,
                capture_output=True,
                timeout=180,
            )
            keys = [uuid.UUID(int=i) for i in (1, 2)]
            for url in (source, candidate):
                with psycopg.connect(url, autocommit=True) as connection:
                    for key in keys:
                        connection.execute("INSERT INTO parents (id) VALUES (%s)", (key,))
                    for i, key in enumerate([keys[0], keys[1], keys[0]], 1):
                        connection.execute(
                            "INSERT INTO children (id, parent_id) VALUES (%s, %s)", (i, key)
                        )
            monkeypatch.setenv(
                "SANKA_GO_SOURCE_TEST_DATABASE_URL",
                source
                if framework == "drf"
                else source.replace("postgresql://", "postgresql+psycopg://", 1),
            )
            monkeypatch.setenv("SANKA_GO_TARGET_TEST_DATABASE_URL", candidate)
            req = request(tmp_path, framework, "fiber")
            req = dataclasses.replace(
                req, command="verify", configuration=req.configuration | {"database_layer": "pgx"}
            )
            result = handle(req)
            assert result.outcome == "success", result.error
            assert [row["id"] for row in result.data["candidate"][0]["body"]] == [1, 3]
            with psycopg.connect(candidate, autocommit=True) as connection:
                connection.execute("UPDATE children SET parent_id=%s WHERE id=3", (keys[1],))
            assert handle(req).outcome == "error"
        finally:
            for name in schemas:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name))
                )

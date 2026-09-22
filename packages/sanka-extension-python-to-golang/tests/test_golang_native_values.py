# SPDX-License-Identifier: Apache-2.0
"""Native rich inputs preserve serializer coercion before database writes."""

from pathlib import Path

import pytest
from sanka_extension_python_to_golang.capture import TARGETS, capture, configuration
from test_golang_drf_validation import drf_field_serializer_source
from test_golang_schema import model_source
from test_golang_validation import native_fastapi_schema_source


def native_source(framework: str) -> str:
    if framework == "drf":
        source = (
            drf_field_serializer_source()
            .replace(
                "serializers.CharField(max_length=40, allow_blank=True, trim_whitespace=False)",
                "serializers.UUIDField()",
            )
            .replace(
                "serializers.CharField(required=False, allow_null=True, "
                "allow_blank=True, trim_whitespace=False)",
                "serializers.UUIDField(required=False, allow_null=True)",
            )
        )
    else:
        source = "from uuid import UUID\n" + native_fastapi_schema_source()
        source = source.replace("name: str", "name: UUID").replace(
            "note: str | None", "note: UUID | None"
        )
    return (
        source.replace('"name": item.name', '"name": str(item.name)')
        .replace('"note": item.note', '"note": (None if item.note is None else str(item.note))')
        .replace("'name': item.name", "'name': str(item.name)")
        .replace("'note': item.note", "'note': (None if item.note is None else str(item.note))")
    )


def native_models(framework: str) -> str:
    source = model_source(framework)
    if framework == "drf":
        return source.replace(
            "models.CharField(max_length=40, unique=True)", "models.UUIDField(unique=True)"
        ).replace("models.TextField(null=True)", "models.UUIDField(null=True)")
    return (
        ("from uuid import UUID\nfrom sqlalchemy import Uuid\n" + source)
        .replace(
            "name: Mapped[str] = mapped_column(String(40), unique=True)",
            "name: Mapped[UUID] = mapped_column(Uuid, unique=True)",
        )
        .replace(
            "note: Mapped[str | None] = mapped_column(Text)",
            "note: Mapped[UUID | None] = mapped_column(Uuid)",
        )
    )


def captured(root: Path, framework: str, target: str = "fiber", source: str | None = None):
    (root / "models.py").write_text(native_models(framework))
    (root / "app.py").write_text(native_source(framework) if source is None else source)
    return capture(
        root,
        configuration(
            {"source_framework": framework, "target_framework": target, "database_layer": "pgx"}
        ),
    )


@pytest.mark.parametrize("framework", ["drf", "fastapi"])
@pytest.mark.parametrize("target", TARGETS)
def test_native_uuid_crud_capture(tmp_path: Path, framework: str, target: str) -> None:
    result = captured(tmp_path, framework, target)
    assert result["gaps"] == []
    assert len(result["routes"]) == 4
    assert capture(tmp_path, result["configuration"]) == result


@pytest.mark.parametrize("framework", ["drf", "fastapi"])
@pytest.mark.parametrize("target", TARGETS)
def test_native_uuid_decoder_parity(tmp_path: Path, framework: str, target: str) -> None:
    import json
    import os
    from uuid import UUID

    from test_golang_validation import assert_go_decoder_parity

    if os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("set SANKA_GO_TESTS=1 for native serializer parity")
    from django.conf import settings

    if not settings.configured:
        settings.configure(USE_I18N=False)
    from pydantic import TypeAdapter
    from rest_framework.fields import UUIDField

    validator = (
        UUIDField().run_validation if framework == "drf" else TypeAdapter(UUID).validate_python
    )
    canonical = "12345678-1234-5678-9abc-123456789abc"
    values = [
        canonical,
        canonical.upper(),
        canonical.replace("-", ""),
        "{" + canonical + "}",
        "urn:uuid:" + canonical,
        "URN:UUID:" + canonical,
        "{" + canonical.replace("-", "") + "}",
        canonical.replace("-", "--"),
        "urn:" + canonical,
        "uuid:" + canonical,
        "{{" + canonical + "}}",
        " " + canonical,
        canonical + "\n",
        "",
        "x" * 32,
        None,
        True,
        False,
        "+" + "1" * 31,
        "0x" + "a" * 30,
        "0x_" + "a" * 29,
        "1_" * 15 + "11",
        "\u0661" * 32,
        "\U0001d7d8" * 32,
        " " + "1" * 30 + " ",
        0,
        1,
        -1,
        2**128 - 1,
        2**128,
        1.0,
        [],
        {},
    ]
    cases = []
    for partial in (False, True):
        for field in ("name", "note"):
            for value in values:
                body = {"name": canonical, "count": 1, "enabled": True, field: value}
                expected = dict(body)
                try:
                    expected[field] = (
                        None if field == "note" and value is None else str(validator(value))
                    )
                except Exception:
                    expected = None
                cases.append(
                    {
                        "body": json.dumps(body),
                        "partial": partial,
                        "valid": expected is not None,
                        "expected": expected,
                    }
                )
    result = captured(tmp_path, framework, target)
    assert result["gaps"] == []
    assert_go_decoder_parity(
        tmp_path, result, cases, "decodeWidget_" + ("drf" if framework == "drf" else "pydantic")
    )


def decimal_source(framework: str) -> str:
    source = native_source(framework)
    if framework == "drf":
        source = source.replace(
            "serializers.IntegerField(min_value=-2147483648, max_value=2147483647)",
            "serializers.DecimalField(max_digits=24, decimal_places=4)",
        )
    else:
        source = "from decimal import Decimal\n" + source
        source = source.replace("count: int", "count: Decimal").replace(
            "ge=-2147483648, le=2147483647", "max_digits=24, decimal_places=4"
        )
    return source.replace('"count": item.count', "\"count\": format(item.count, 'f')").replace(
        "'count': item.count", "'count': format(item.count, 'f')"
    )


def decimal_capture(root: Path, framework: str, target: str = "fiber"):
    result = captured(root, framework, target, decimal_source(framework))
    models = native_models(framework)
    if framework == "drf":
        models = models.replace(
            "models.IntegerField()", "models.DecimalField(max_digits=24, decimal_places=4)"
        )
    else:
        models = "from decimal import Decimal\nfrom sqlalchemy import Numeric\n" + models.replace(
            "count: Mapped[int] = mapped_column(Integer)",
            "count: Mapped[Decimal] = mapped_column(Numeric(24, 4))",
        )
    (root / "models.py").write_text(models)
    return capture(root, result["configuration"])


@pytest.mark.parametrize("framework", ["drf", "fastapi"])
def test_native_decimal_capture(tmp_path: Path, framework: str) -> None:
    result = decimal_capture(tmp_path, framework)
    assert result["gaps"] == []


@pytest.mark.parametrize("framework", ["drf", "fastapi"])
@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("precision,scale", [(24, 4), (8, 0), (8, 8), (38, 10)])
def test_native_decimal_decoder_parity(
    tmp_path: Path, framework: str, target: str, precision: int, scale: int
) -> None:
    import json
    import os
    from decimal import ROUND_HALF_UP, Decimal, localcontext
    from typing import Annotated

    from test_golang_validation import assert_go_decoder_parity

    if os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("set SANKA_GO_TESTS=1 for native serializer parity")
    from django.conf import settings

    if not settings.configured:
        settings.configure(USE_I18N=False)
    from pydantic import Field, TypeAdapter
    from rest_framework.fields import DecimalField

    validator = (
        DecimalField(max_digits=precision, decimal_places=scale).run_validation
        if framework == "drf"
        else TypeAdapter(
            Annotated[Decimal, Field(max_digits=precision, decimal_places=scale)]
        ).validate_python
    )
    values = [
        "1.00000000000000000000000000001",
        "1.23450000000000000000000000001",
        "1.23449999999999999999999999999",
        "1",
        "1.2345",
        "1.23450",
        "1.23456",
        "12345678901234567890.1234",
        "123456789012345678901",
        "0.00000",
        "0e-999",
        "0e-1000000000000000000",
        "1e-1000000",
        "0e999",
        "1e-999",
        "1e999",
        "-0",
        "1_2.3",
        "__1__",
        "١٢.٣",
        " 1 ",
        "\x1c1\x1c",
        "+1.20",
        "00.10000",
        "NaN",
        "Infinity",
        "",
        "1..2",
        True,
        None,
        [],
        {},
        1,
        1.0,
        0.1,
        1e-5,
        -0.0,
        1e19,
        1e20,
    ]
    cases = []
    for partial in (False, True):
        for value in values:
            body = {"name": "12345678-1234-5678-9abc-123456789abc", "count": value, "enabled": True}
            try:
                number = validator(value)
                if framework == "drf":
                    with localcontext() as context:
                        context.prec = 1000
                        number = number.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)
                    text = format(number, "f")
                else:
                    # Preserve the original exact coefficient/exponent until PostgreSQL
                    # applies the column scale, just like SQLAlchemy/psycopg.
                    sign, digits, exponent = number.as_tuple()
                    text = ("-" if sign else "") + "".join(map(str, digits)) + "e" + str(exponent)
                expected = body | {"count": text}
            except Exception:
                expected = None
            cases.append(
                {
                    "body": json.dumps(body),
                    "partial": partial,
                    "valid": expected is not None,
                    "expected": expected,
                }
            )
    result = decimal_capture(tmp_path, framework, target)
    for name in ("app.py", "models.py"):
        path = tmp_path / name
        path.write_text(
            path.read_text()
            .replace("max_digits=24", f"max_digits={precision}")
            .replace("decimal_places=4", f"decimal_places={scale}")
            .replace("Numeric(24, 4)", f"Numeric({precision}, {scale})")
        )
    result = capture(tmp_path, result["configuration"])
    assert result["gaps"] == []
    assert_go_decoder_parity(
        tmp_path, result, cases, "decodeWidget_" + ("drf" if framework == "drf" else "pydantic")
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_import",
        "wrong_import",
        "wrapped_value",
        "custom_validator",
        "different_scale",
        "strict",
    ],
)
def test_native_values_fail_closed(tmp_path: Path, mutation: str) -> None:
    result = decimal_capture(tmp_path, "fastapi")
    source = (tmp_path / "app.py").read_text()
    before, after = {
        "missing_import": ("from uuid import UUID", ""),
        "wrong_import": ("from uuid import UUID", "from counterfeit import UUID"),
        "wrapped_value": ('name=data["name"]', 'name=UUID(data["name"] )'),
        "custom_validator": (
            "class WidgetInput(BaseModel):",
            "class WidgetInput(BaseModel):\n    def model_post_init(self, context):\n"
            "        self.count = 0",
        ),
        "different_scale": ("decimal_places=4", "decimal_places=3"),
        "strict": ("max_digits=24", "strict=True, max_digits=24"),
    }[mutation]
    assert before in source
    (tmp_path / "app.py").write_text(source.replace(before, after))
    assert capture(tmp_path, result["configuration"])["gaps"]


@pytest.mark.parametrize("repository", ["direct", "function", "class"])
def test_native_values_async_repository(tmp_path: Path, repository: str) -> None:
    from test_golang_async_persistence import injected_source

    result = decimal_capture(tmp_path, "fastapi")
    (tmp_path / "app.py").write_text(injected_source(repository, decimal_source("fastapi")))
    asynchronous = capture(tmp_path, result["configuration"])
    assert asynchronous["gaps"] == []
    assert asynchronous["routes"] == result["routes"]


@pytest.mark.parametrize("framework", ["drf", "fastapi", "fastapi-async"])
@pytest.mark.parametrize("target", TARGETS)
def test_native_values_postgres_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, framework: str, target: str
) -> None:
    import dataclasses
    import json
    import os
    import uuid

    import psycopg
    from psycopg import sql
    from sanka_extension_python_to_golang.adapter import handle
    from test_golang_async_persistence import injected_source
    from test_golang_schema import generate, schema_dsn
    from test_python_to_golang import request

    asynchronous = framework.endswith("-async")
    framework = framework.removesuffix("-async")
    decimal_capture(tmp_path, framework, target)
    source_text = decimal_source(framework)
    if asynchronous:
        source_text = injected_source("class", source_text)
    models = (tmp_path / "models.py").read_text()
    body = {
        "name": "12345678-1234-5678-9ABC-123456789ABC",
        "count": "12345678901234567890.1234",
        "enabled": True,
    }
    invalid = 400 if framework == "drf" else 422
    scenarios = [
        {
            "id": "invalid-uuid",
            "method": "POST",
            "path": "/widgets",
            "body": body | {"name": "bad"},
            "expected_status": invalid,
        },
        {
            "id": "invalid-decimal",
            "method": "POST",
            "path": "/widgets",
            "body": body | {"count": "1.23456"},
            "expected_status": invalid,
        },
        {
            "id": "create",
            "method": "POST",
            "path": "/widgets",
            "body": body,
            "expected_status": 201,
        },
        {
            "id": "uuid-null",
            "method": "PATCH",
            "path": "/widgets/1",
            "body": {"note": None},
            "expected_status": 200,
        },
        {
            "id": "signed-zero",
            "method": "PATCH",
            "path": "/widgets/1",
            "body": {"count": "-0"},
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
            "id": "bad-patch",
            "method": "PATCH",
            "path": "/widgets/1",
            "body": {"count": "NaN"},
            "expected_status": invalid,
        },
        {
            "id": "replace",
            "method": "PUT",
            "path": "/widgets/1",
            "body": body | {"count": "١٢.٣", "note": "urn:uuid:" + body["name"]},
            "expected_status": 200,
        },
        {"id": "delete", "method": "DELETE", "path": "/widgets/1", "expected_status": 204},
        {"id": "delete-again", "method": "DELETE", "path": "/widgets/1", "expected_status": 404},
        {
            "id": "recreate",
            "method": "POST",
            "path": "/widgets",
            "body": body | {"count": 0.1},
            "expected_status": 201,
        },
    ]
    (tmp_path / "sanka-verify.json").write_text(json.dumps({"scenarios": scenarios}))
    output = generate(tmp_path, framework, target, app_source=source_text, model_text=models)
    planned = json.loads((tmp_path / ".sanka/go/plan.json").read_text())["capture"]
    assert capture(tmp_path, planned["configuration"]) == planned
    if os.getenv("SANKA_GO_TESTS") != "1" or not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN"):
        pytest.skip("requires explicitly configured PostgreSQL fixtures and Go")
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    schemas = ["native_values_" + uuid.uuid4().hex for _ in range(2)]
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            for name in schemas:
                admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
            source_url, target_url = [schema_dsn(dsn, name) for name in schemas]
            if framework != "drf":
                source_url = source_url.replace("postgresql://", "postgresql+psycopg://", 1)
            monkeypatch.setenv("SANKA_GO_SOURCE_TEST_DATABASE_URL", source_url)
            monkeypatch.setenv("SANKA_GO_TARGET_TEST_DATABASE_URL", target_url)
            req = request(tmp_path, framework, target)
            result = handle(
                dataclasses.replace(
                    req,
                    command="verify",
                    configuration=req.configuration | {"database_layer": "pgx"},
                )
            )
            report_path = tmp_path / ".sanka/go/verify.json"
            report = json.loads(report_path.read_text()) if report_path.exists() else {}
            if result.outcome != "success":
                print(json.dumps(report, sort_keys=True))
            assert result.outcome == "success", result.error
            assert result.data["source"] == result.data["candidate"]
            observed = {row["id"]: row for row in result.data["candidate"]}
            assert observed["create"]["body"]["count"] == "12345678901234567890.1234"
            assert observed["signed-zero"]["body"]["count"] == (
                "-0.0000" if framework == "drf" else "0.0000"
            )
            assert observed["absent"]["body"]["count"] == "0.0000"
            assert observed["recreate"]["body"]["id"] == "2"
            if framework == "fastapi":
                assert_decimal_database_boundaries(output, source_url, target_url)

        finally:
            for name in schemas:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name))
                )


@pytest.mark.parametrize("framework", ["fastapi", "flask"])
def test_unqualified_strict_rich_schema_is_a_gap(tmp_path: Path, framework: str) -> None:
    from test_golang_validation import schema_source

    result = captured(tmp_path, framework, source=schema_source(framework))
    assert result["gaps"]


@pytest.mark.parametrize("framework", ["drf", "fastapi"])
def test_native_required_field_is_not_optional_because_of_orm_default(
    tmp_path: Path, framework: str
) -> None:
    import json
    import os

    from test_golang_validation import assert_go_decoder_parity

    if os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("requires Go")
    result = captured(tmp_path, framework)
    path = tmp_path / "models.py"
    path.write_text(
        path.read_text()
        .replace("models.IntegerField()", "models.IntegerField(default=7)")
        .replace("mapped_column(Integer)", "mapped_column(Integer, default=7)")
    )
    import ast

    class Defaults(ast.NodeTransformer):
        def visit_If(self, node):
            if isinstance(node.test, ast.Compare) and any(
                isinstance(op, ast.In) for op in node.test.ops
            ):
                return node
            return self.generic_visit(node)

        def visit_Subscript(self, node):
            if ast.unparse(node) == "data['count']":
                return ast.copy_location(ast.parse("data.get('count', 7)", mode="eval").body, node)
            return node

    app = tmp_path / "app.py"
    app.write_text(
        ast.unparse(ast.fix_missing_locations(Defaults().visit(ast.parse(app.read_text()))))
    )
    result = capture(tmp_path, result["configuration"])
    assert result["gaps"] == []
    body = {"name": "12345678-1234-5678-9abc-123456789abc", "enabled": True}
    cases = [
        {
            "body": json.dumps(body),
            "partial": partial,
            "valid": partial,
            "expected": body if partial else None,
        }
        for partial in (False, True)
    ]
    assert_go_decoder_parity(
        tmp_path, result, cases, "decodeWidget_" + ("drf" if framework == "drf" else "pydantic")
    )


def assert_decimal_database_boundaries(output: Path, source_url: str, target_url: str) -> None:
    """Compare the generated write against psycopg's original Decimal binding."""
    import json
    import os
    import subprocess
    from decimal import Decimal
    from uuid import UUID

    import psycopg

    cases = []
    with psycopg.connect(
        source_url.replace("postgresql+psycopg://", "postgresql://"), autocommit=True
    ) as connection:
        for index, value in enumerate(
            [
                "1.23449999999999999999999999999",
                "0e-999",
                "1e-1000000",
                "0e-1000000000000000000",
                "0e999999999999999999",
            ]
        ):
            body = {"name": str(UUID(int=100 + index)), "count": value, "enabled": True}
            try:
                saved = connection.execute("SELECT %s::numeric(24,4)", [Decimal(value)]).fetchone()[
                    0
                ]
                expected = format(saved, "f")
            except psycopg.Error:
                expected = None
            cases.append(
                {
                    "body": json.dumps(body),
                    "valid": expected is not None,
                    "expected": expected or "",
                }
            )
    (output / "numeric-boundaries.json").write_text(json.dumps(cases))
    (output / "numeric_boundaries_test.go").write_text("""package backend
import ("context"; "encoding/json"; "os"; "testing"; "github.com/jackc/pgx/v5/pgxpool")
func TestNumericDatabaseBoundaries(t *testing.T) {
    ctx := context.Background()
    pool, err := pgxpool.New(ctx, os.Getenv("DATABASE_URL"))
    if err != nil { t.Fatal(err) }; defer pool.Close()
    data, err := os.ReadFile("numeric-boundaries.json"); if err != nil { t.Fatal(err) }
    var cases []struct { Body string; Valid bool; Expected string }
    if err := json.Unmarshal(data, &cases); err != nil { t.Fatal(err) }
    var before, after int
    if err := pool.QueryRow(ctx, "SELECT count(*) FROM widgets").Scan(&before); err != nil {
        t.Fatal(err)
    }
    successes := 0
    for _, c := range cases {
        item, err := writeRow0(ctx, pool, []byte(c.Body))
        if (err == nil) != c.Valid { t.Fatalf("%s: valid=%v err=%v", c.Body, c.Valid, err) }
        if err == nil {
            successes++
            if string(item.Count) != c.Expected {
                t.Fatalf("%s: %s != %s", c.Body, item.Count, c.Expected)
            }
        }
    }
    if err := pool.QueryRow(ctx, "SELECT count(*) FROM widgets").Scan(&after); err != nil {
        t.Fatal(err)
    }
    if after-before != successes { t.Fatal("failed numeric writes changed persisted rows") }
}
""")
    result = subprocess.run(
        ["go", "test", "-mod=readonly", "-p=2", "-run", "TestNumericDatabaseBoundaries", "."],
        cwd=output,
        env=os.environ
        | {"DATABASE_URL": target_url, "GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("framework", ["drf", "fastapi"])
def test_nullable_native_decimal(tmp_path: Path, framework: str) -> None:
    import ast
    import json
    import os

    from test_golang_validation import assert_go_decoder_parity

    result = decimal_capture(tmp_path, framework)
    model = tmp_path / "models.py"
    model.write_text(
        model.read_text()
        .replace(
            "models.DecimalField(max_digits=24, decimal_places=4)",
            "models.DecimalField(max_digits=24, decimal_places=4, null=True)",
        )
        .replace("count: Mapped[Decimal]", "count: Mapped[Decimal | None]")
    )
    tree = ast.parse(decimal_source(framework))
    tree.body = [
        node
        for node in tree.body
        if not (
            isinstance(node, ast.FunctionDef)
            and node.name not in {"create_widget", "invalid_request"}
        )
        and not (isinstance(node, ast.ClassDef) and node.name == "WidgetPatch")
    ]
    for node in tree.body:
        if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "urlpatterns":
            node.value = ast.parse("[path('widgets', create_widget)]", mode="eval").body
    source = (
        ast.unparse(tree)
        .replace("count: Decimal = Field(", "count: Decimal | None = Field(default=None, ")
        .replace(
            "count = serializers.DecimalField(",
            "count = serializers.DecimalField(required=False, allow_null=True, ",
        )
    )
    source = source.replace("count=data['count']", "count=data.get('count')").replace(
        "format(item.count, 'f')", "(None if item.count is None else format(item.count, 'f'))"
    )
    (tmp_path / "app.py").write_text(source)
    result = capture(tmp_path, result["configuration"])
    assert result["gaps"] == []
    if os.getenv("SANKA_GO_TESTS") == "1":
        body = {"name": "12345678-1234-5678-9abc-123456789abc", "enabled": True}
        count = "1.2500" if framework == "drf" else "125e-2"
        cases = [
            {
                "body": json.dumps(body | fields),
                "partial": False,
                "valid": True,
                "expected": body | expected,
            }
            for fields, expected in [
                ({}, {}),
                ({"count": None}, {"count": None}),
                ({"count": "1.25"}, {"count": count}),
            ]
        ]
        assert_go_decoder_parity(
            tmp_path, result, cases, "decodeWidget_" + ("drf" if framework == "drf" else "pydantic")
        )


@pytest.mark.parametrize(
    "framework,statement",
    [
        ("fastapi", "from uuid import UUID"),
        ("fastapi", "from pydantic import BaseModel, Field"),
        ("drf", "from rest_framework import serializers"),
    ],
)
def test_native_schema_imports_must_precede_declaration(
    tmp_path: Path, framework: str, statement: str
) -> None:
    source = native_source(framework).replace(statement, "") + "\n" + statement + "\n"
    assert captured(tmp_path, framework, source=source)["gaps"]


def test_native_serializer_rejects_raw_optional_input(tmp_path: Path) -> None:
    import ast

    source = ast.parse(native_source("drf"))

    class RawInput(ast.NodeTransformer):
        def visit_Attribute(self, node):
            if ast.unparse(node) == "data.get":
                return ast.copy_location(ast.parse("request.data.get", mode="eval").body, node)
            return self.generic_visit(node)

    text = ast.unparse(ast.fix_missing_locations(RawInput().visit(source)))
    assert "request.data.get" in text
    assert captured(tmp_path, "drf", source=text)["gaps"]


def test_decimal_scan_retains_zero_scale(tmp_path: Path) -> None:
    import os
    import subprocess

    from test_golang_schema import generate

    if os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("requires native Go")
    decimal_capture(tmp_path, "fastapi")
    output = generate(
        tmp_path,
        "fastapi",
        "fiber",
        app_source=decimal_source("fastapi"),
        model_text=(tmp_path / "models.py").read_text(),
    )
    assert '"count"::text' in (output / "app.go").read_text().replace('\\"', '"')
    (output / "numeric_scale_test.go").write_text("""package backend
import ("testing"; "github.com/jackc/pgx/v5/pgtype")
func TestZeroScale(t *testing.T) {
    for _, want := range []string{"0", "0.0000", "0.0000000000"} {
        var value DecimalValue
        codec := pgtype.NewMap()
        err := codec.Scan(pgtype.TextOID, pgtype.TextFormatCode, []byte(want), &value)
        if err != nil || string(value) != want { t.Fatalf("%s: %s %v", want, value, err) }
        var nullable *DecimalValue
        err = codec.Scan(pgtype.TextOID, pgtype.TextFormatCode, []byte(want), &nullable)
        if err != nil || nullable == nil || string(*nullable) != want { t.Fatal(want, err) }
        err = codec.Scan(pgtype.TextOID, pgtype.TextFormatCode, nil, &nullable)
        if err != nil || nullable != nil { t.Fatal("NULL numeric", err) }
    }
}
""")
    result = subprocess.run(
        ["go", "test", "-mod=readonly", "-p=2", "-run", "TestZeroScale", "./..."],
        cwd=output,
        capture_output=True,
        text=True,
        timeout=180,
        env=os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"},
    )
    assert result.returncode == 0, result.stdout + result.stderr

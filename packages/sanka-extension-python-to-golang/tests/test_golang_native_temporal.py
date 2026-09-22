# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""Native temporal and JSON bodies retain source validation and storage semantics."""

import json
import os
from datetime import UTC, date
from pathlib import Path

import pytest
from sanka_extension_python_to_golang.capture import TARGETS, capture, configuration
from test_golang_drf_validation import drf_field_serializer_source
from test_golang_schema import model_source
from test_golang_validation import assert_go_decoder_parity, native_fastapi_schema_source


def temporal_source(framework):
    if framework == "drf":
        source = (
            drf_field_serializer_source()
            .replace(
                "serializers.CharField(max_length=40, allow_blank=True, trim_whitespace=False)",
                "serializers.DateField(input_formats=['iso-8601'])",
            )
            .replace(
                "serializers.IntegerField(min_value=-2147483648, max_value=2147483647)",
                "serializers.DateTimeField(input_formats=['iso-8601'], default_timezone=timezone.utc)",
            )
            .replace(
                "serializers.CharField(required=False, allow_null=True, allow_blank=True, trim_whitespace=False)",
                "serializers.JSONField(required=False, allow_null=True)",
            )
        )
    else:
        source = (
            native_fastapi_schema_source()
            .replace(
                "from pydantic import BaseModel, Field",
                "from pydantic import BaseModel, Field, AwareDatetime, JsonValue",
            )
            .replace("name: str", "name: date")
            .replace("note: str | None", "note: JsonValue | None")
        )
        source = source.replace(
            "count: int = Field(ge=-2147483648, le=2147483647)", "count: AwareDatetime"
        )
        source = source.replace(
            "count: int = Field(default=None, ge=-2147483648, le=2147483647)",
            "count: AwareDatetime = None",
        )
    source = "from datetime import date, timezone\n" + source
    for quote in ("'", '"'):
        source = source.replace(
            f"{quote}name{quote}: item.name", f"{quote}name{quote}: item.name.isoformat()"
        )
        source = source.replace(
            f"{quote}count{quote}: item.count",
            f"{quote}count{quote}: item.count.astimezone(timezone.utc).isoformat(timespec='microseconds')",
        )
    return source


def temporal_models(framework):
    source = model_source(framework)
    if framework == "drf":
        return (
            source.replace(
                "models.CharField(max_length=40, unique=True)", "models.DateField(unique=True)"
            )
            .replace("models.IntegerField()", "models.DateTimeField()")
            .replace("models.TextField(null=True)", "models.JSONField(null=True)")
        )
    return (
        (
            "from datetime import date, datetime\nfrom sqlalchemy import Date, DateTime\nfrom sqlalchemy.dialects.postgresql import JSONB\n"
            + source
        )
        .replace(
            "name: Mapped[str] = mapped_column(String(40), unique=True)",
            "name: Mapped[date] = mapped_column(Date, unique=True)",
        )
        .replace(
            "count: Mapped[int] = mapped_column(Integer)",
            "count: Mapped[datetime] = mapped_column(DateTime(timezone=True))",
        )
        .replace(
            "note: Mapped[str | None] = mapped_column(Text)",
            "note: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))",
        )
    )


def temporal_capture(root, framework, target="fiber"):
    (root / "app.py").write_text(temporal_source(framework))
    (root / "models.py").write_text(temporal_models(framework))
    return capture(
        root,
        configuration(
            {"source_framework": framework, "target_framework": target, "database_layer": "pgx"}
        ),
    )


@pytest.mark.parametrize("framework", ["drf", "fastapi"])
def test_native_temporal_capture(tmp_path, framework):
    result = temporal_capture(tmp_path, framework)
    assert result["gaps"] == []
    assert len(result["routes"]) == 4
    assert capture(tmp_path, result["configuration"]) == result


@pytest.mark.parametrize("framework", ["drf", "fastapi"])
@pytest.mark.parametrize("target", TARGETS)
def test_native_temporal_decoder(tmp_path: Path, framework, target):
    if os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("requires native Go")
    from django.conf import settings

    if not settings.configured:
        settings.configure(USE_I18N=False, USE_TZ=True)
    from pydantic import AwareDatetime, JsonValue, TypeAdapter
    from rest_framework.fields import DateField, DateTimeField, JSONField

    validators = (
        {
            "name": DateField(input_formats=["iso-8601"]).run_validation,
            "count": DateTimeField(input_formats=["iso-8601"], default_timezone=UTC).run_validation,
            "note": JSONField(allow_null=True).run_validation,
        }
        if framework == "drf"
        else {
            "name": TypeAdapter(date).validate_python,
            "count": TypeAdapter(AwareDatetime).validate_python,
            "note": TypeAdapter(JsonValue).validate_python,
        }
    )
    values = {
        "name": [
            "2024-02-29",
            "2023-02-29",
            "0000-01-01",
            "9999-12-31",
            "20240229",
            "2024-2-9",
            "2024-W09-4",
            "2024W094",
            "2024-W09",
            "2024-02-29T00:00:00Z",
            "2024-02-29T00:00:01Z",
            "2024-02-29T00:00:00+07:00",
            0,
            86400,
            86400000,
            1709164800,
            1709164800000,
            "1709164800",
            0.5,
            True,
            None,
            {},
            "2024-02-29\n",
        ],
        "count": [
            "2024-02-29T12:34:56Z",
            "2024-02-29T12:34:56.123456789+07:00",
            "2024-02-29 12:34:56-03:30",
            "2024-02-29t12:34:56z",
            "2024-02-29T12:34Z",
            "2024-02-29T12Z",
            "2024-02-29",
            "20240229T123456Z",
            "2024-W09-4T12:34:56Z",
            "2024-02-29T12:34:56",
            "2024-02-29T24:00:00Z",
            "2024-02-29T12:34:60Z",
            "2024-02-29T12:34:56+24:00",
            "0001-01-01T00:00:00+01:00",
            "9999-12-31T23:59:59-01:00",
            0,
            1.1234567,
            -1.1234567,
            20000000001,
            "1709164800",
            True,
            None,
            [],
        ],
        "note": [
            None,
            {},
            [],
            {"nested": [True, None, 1, 1.25, {"k": "v"}]},
            2**64,
            -(2**64),
            1.2345678901234567,
            "",
            "null",
            False,
        ],
    }
    values["name"] += [
        "2024-02-29T00:00:00",
        "2024-02-29 00:00:00",
        "2024-W09-4\n",
        "20240229\n",
        "\uff12\uff10\uff12\uff14-\uff10\uff12-\uff12\uff19",
    ]
    values["count"] += [
        f"{d}{sep}{t}{z}"
        for d in ("2024-02-29", "20240229", "2024-W09-4", "2024-2-9")
        for sep in ("T", " ", "_", "x")
        for t in ("12:34:56", "123456", "12:3456", "1234:56", "1:2:3", "12", "12.5", "12:34,5")
        for z in ("", "Z", "+07:00", "+0700", "+07", "+01:02:03.25", "+00:00:00.5", "+00:60")
    ]
    values["count"] += [
        0.0000005,
        -0.0000005,
        20000000000.5,
        -20000000000.5,
        -20000000001,
        -20000000001.25,
        "-1.1234567",
        " 123",
        1e20,
        1e-7,
    ]
    values["name"] += ["2024-W094", "2024W09-4", "-0.0000005"]
    values["count"] += ["+1", ".5", "-.5", "1.", "-0.0000005", "1e3"]
    cases = []
    for partial in (False, True):
        for field, inputs in values.items():
            for value in inputs:
                body = {
                    "name": "2024-02-29",
                    "count": "2024-02-29T12:34:56Z",
                    "enabled": True,
                    field: value,
                }
                try:
                    expected = dict(body)
                    expected["name"] = validators["name"](body["name"]).isoformat()
                    expected["count"] = (
                        validators["count"](body["count"])
                        .astimezone(UTC)
                        .isoformat(timespec="microseconds")
                    )
                    if "note" in body:
                        expected["note"] = validators["note"](body["note"])
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
    result = temporal_capture(tmp_path, framework, target)
    assert result["gaps"] == []
    assert_go_decoder_parity(
        tmp_path, result, cases, "decodeWidget_" + ("drf" if framework == "drf" else "pydantic")
    )


def temporal_project(root: Path, framework: str, target: str) -> dict:
    """Packaged fixture shared by protocol replay and installed-CLI qualification."""
    from test_golang_async_persistence import injected_source

    async_ = framework.endswith("-async")
    framework = framework.removesuffix("-async")
    package = root / "src/backend"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "models.py").write_text(temporal_models(framework))
    app = temporal_source(framework)
    if async_:
        app = injected_source("class", app)
    (package / "main.py").write_text(app.replace("from models import", "from .models import"))
    (root / "tests").mkdir()
    (root / "tests/test_original.py").write_text(
        "raise RuntimeError('must not execute original tests')"
    )
    body = {
        "name": "2024-02-29",
        "count": "2024-02-29T12:34:56.123456+07:00",
        "enabled": True,
        "note": {"nested": [None, 1.25, True, 2**64]},
    }
    scenarios = [
        {
            "id": "bad-date",
            "method": "POST",
            "path": "/widgets",
            "body": body | {"name": "2023-02-29"},
            "expected_status": 400 if framework == "drf" else 422,
        },
        {
            "id": "bad-time",
            "method": "POST",
            "path": "/widgets",
            "body": body | {"count": "not a time"},
            "expected_status": 400 if framework == "drf" else 422,
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
            "body": body | {"count": "2024-03-01T00:00:00-03:30", "note": ["null", None, {}]},
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
        "source_framework": framework,
        "target_framework": target,
        "source_file": "src/backend/main.py",
        "models_file": "src/backend/models.py",
        "database_layer": "pgx",
    }


@pytest.mark.parametrize("framework", ["drf", "fastapi", "fastapi-async"])
@pytest.mark.parametrize("target", TARGETS)
def test_native_temporal_postgres(tmp_path, monkeypatch, framework, target):
    import dataclasses
    import uuid

    from sanka_extension_python_to_golang.adapter import handle
    from test_golang_schema import schema_dsn
    from test_python_to_golang import request

    config = temporal_project(tmp_path, framework, target)
    req = dataclasses.replace(
        request(tmp_path, config["source_framework"], target), configuration=config
    )
    plan = handle(req)
    assert plan.outcome == "success", plan.error
    assert plan.data == handle(req).data
    assert (
        handle(
            dataclasses.replace(
                req,
                command="apply",
                reviewed_plan_hash="reviewed",
                configuration=config | {"extension_plan_hash": plan.data["plan_hash"]},
            )
        ).outcome
        == "success"
    )
    if os.getenv("SANKA_GO_TESTS") != "1" or not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN"):
        pytest.skip("plan/apply passed; PostgreSQL fixture and Go required for replay")
    import psycopg
    from psycopg import sql

    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    schemas = ["temporal_" + uuid.uuid4().hex for _ in range(2)]
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            for name in schemas:
                admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
            source, target_url = [schema_dsn(dsn, name) for name in schemas]
            if framework != "drf":
                source = source.replace("postgresql://", "postgresql+psycopg://", 1)
            monkeypatch.setenv("SANKA_GO_SOURCE_TEST_DATABASE_URL", source)
            monkeypatch.setenv("SANKA_GO_TARGET_TEST_DATABASE_URL", target_url)
            verified = handle(dataclasses.replace(req, command="verify"))
            if verified.outcome != "success":
                report = tmp_path / ".sanka/go/verify.json"
                if report.exists():
                    print(report.read_text())
            assert verified.outcome == "success", verified.error
            assert verified.data["source"] == verified.data["candidate"]
            observed = {row["id"]: row for row in verified.data["candidate"]}
            assert observed["create"]["body"]["count"] == "2024-02-29T05:34:56.123456+00:00"
            assert observed["null"]["body"]["note"] is None
            assert observed["absent"]["body"] == observed["null"]["body"]
            assert observed["recreate"]["body"]["id"] == "2"
        finally:
            for name in schemas:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name))
                )


@pytest.mark.parametrize(
    "framework,old,new",
    [
        ("drf", "default_timezone=timezone.utc", "default_timezone=None"),
        ("drf", "input_formats=['iso-8601']", "input_formats=['%d/%m/%Y']"),
        (
            "drf",
            "serializers.JSONField(required=False, allow_null=True)",
            "serializers.JSONField(required=False, allow_null=True, binary=True)",
        ),
        ("fastapi", "count: AwareDatetime", "count: datetime"),
        ("fastapi", "note: JsonValue | None", "note: dict | None"),
    ],
)
def test_native_temporal_unknown_policies_block_capture(tmp_path, framework, old, new):
    temporal_capture(tmp_path, framework)
    (tmp_path / "app.py").write_text(temporal_source(framework).replace(old, new))
    result = capture(
        tmp_path, configuration({"source_framework": framework, "database_layer": "pgx"})
    )
    assert result["gaps"]


@pytest.mark.parametrize("none_as_null", [False, True])
@pytest.mark.parametrize("nullable", [False, True])
def test_native_json_null_decoder(tmp_path, none_as_null, nullable):
    if os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("requires native Go")
    temporal_capture(tmp_path, "fastapi")
    model = temporal_models("fastapi").replace("none_as_null=True", f"none_as_null={none_as_null}")
    source = temporal_source("fastapi")
    if not nullable:
        model = model.replace("note: Mapped[dict | None]", "note: Mapped[dict]")
        source = source.replace("note: JsonValue | None = None", "note: JsonValue")
        # Partial schemas use an optional default without weakening create requiredness.
        start = source.index("class WidgetPatch")
        source = source[:start] + source[start:].replace(
            "note: JsonValue", "note: JsonValue = None", 1
        )
        source = source.replace("data.get('note')", "data['note']").replace(
            'data.get("note")', 'data["note"]'
        )
    (tmp_path / "models.py").write_text(model)
    (tmp_path / "app.py").write_text(source)
    result = capture(
        tmp_path, configuration({"source_framework": "fastapi", "database_layer": "pgx"})
    )
    assert result["gaps"] == []
    body = {"name": "2024-02-29", "count": "2024-02-29T12:34:56Z", "enabled": True, "note": None}
    expected = body | {"count": "2024-02-29T12:34:56.000000+00:00"}
    assert_go_decoder_parity(
        tmp_path,
        result,
        [{"body": json.dumps(body), "partial": False, "valid": True, "expected": expected}],
        "decodeWidget_pydantic",
    )


def test_native_drf_timezone_import_must_precede_schema(tmp_path):
    temporal_capture(tmp_path, "drf")
    text = temporal_source("drf").replace("from datetime import date, timezone\n", "")
    (tmp_path / "app.py").write_text(text + "\nfrom datetime import date, timezone\n")
    assert capture(tmp_path, configuration({"source_framework": "drf", "database_layer": "pgx"}))[
        "gaps"
    ]

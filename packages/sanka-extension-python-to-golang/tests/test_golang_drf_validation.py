# SPDX-License-Identifier: Apache-2.0
"""Qualified strict DRF serializers preserve validation and validated values."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from textwrap import indent

import pytest
from sanka_extension_python_to_golang.capture import TARGETS, capture, configuration
from sanka_extension_python_to_golang.render import render
from test_golang_schema import model_source
from test_golang_validation import assert_go_decoder_parity, validation_cases
from test_golang_writes import combine_drf_methods, drf_write_source, widget_validation


def serializer_source() -> str:
    error = 'raise ValidationError("invalid request body")'
    return (
        "from rest_framework.serializers import BaseSerializer, ValidationError\n"
        "class WidgetInput(BaseSerializer):\n"
        "    def to_internal_value(self, data):\n"
        "        if self.partial:\n"
        + indent(widget_validation("data", True, error), "            ")
        + "        else:\n"
        + indent(widget_validation("data", False, error), "            ")
        + "        return data\n"
    )


def drf_serializer_source() -> str:
    tree = ast.parse(drf_write_source(combined=False))

    class InputData(ast.NodeTransformer):
        def visit_Attribute(self, node: ast.Attribute) -> ast.expr:
            if ast.unparse(node) == "request.data":
                return ast.copy_location(ast.Name(id="data", ctx=ast.Load()), node)
            return node

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name != "delete_widget":
            remaining = ast.Module(body=node.body[1:], type_ignores=[])
            InputData().visit(remaining)
            partial = node.name == "patch_widget"
            node.body = (
                ast.parse(
                    f"serializer = WidgetInput(data=request.data, partial={partial})\n"
                    "if not serializer.is_valid():\n"
                    '    return Response({"error": "invalid request body"}, status=400)\n'
                    "data = serializer.validated_data\n"
                ).body
                + remaining.body
            )
    return serializer_source() + combine_drf_methods(ast.unparse(tree))


def field_serializer_source() -> str:
    return (
        """from rest_framework import serializers
class WidgetInput(serializers.Serializer):
    name = serializers.CharField(max_length=40, allow_blank=True, trim_whitespace=False)
    count = serializers.IntegerField(min_value=-2147483648, max_value=2147483647)
    enabled = serializers.BooleanField()
"""
        "    note = serializers.CharField(required=False, allow_null=True, "
        "allow_blank=True, trim_whitespace=False)\n"
    )


def drf_field_serializer_source() -> str:
    return field_serializer_source() + drf_serializer_source().removeprefix(serializer_source())


def capture_source(
    root: Path, target: str, source: str | None = None, models: str | None = None
) -> dict:
    (root / "app.py").write_text(source if source is not None else drf_serializer_source())
    (root / "models.py").write_text(models if models is not None else model_source("drf"))
    return capture(
        root,
        configuration(
            {"source_framework": "drf", "target_framework": target, "database_layer": "pgx"}
        ),
    )


@pytest.mark.parametrize("target", TARGETS)
def test_serializer_capture_matches_manual_validation(tmp_path: Path, target: str) -> None:
    captured = capture_source(tmp_path, target)
    assert captured["gaps"] == []
    manual = capture_source(tmp_path, target, drf_write_source())
    assert captured["routes"] == manual["routes"]
    assert captured["source_digest"] != manual["source_digest"]
    assert render(captured)["app.go"] == render(manual)["app.go"]


@pytest.mark.parametrize("target", TARGETS)
def test_field_serializer_capture(tmp_path: Path, target: str) -> None:
    captured = capture_source(tmp_path, target, drf_field_serializer_source())
    assert captured["gaps"] == []
    assert [route["write"].get("validation") for route in captured["routes"][:3]] == [
        {"kind": "drf", "schema": "WidgetInput", "partial": False},
        {"kind": "drf", "schema": "WidgetInput", "partial": True},
        {"kind": "drf", "schema": "WidgetInput", "partial": False},
    ]
    assert "decodeWidget_drf" in render(captured)["app.go"]
    assert capture(tmp_path, captured["configuration"]) == captured


@pytest.mark.parametrize(
    "before,after",
    [
        ("max_length=40", "max_length=41"),
        ("allow_blank=True", "allow_blank=False"),
        ("trim_whitespace=False", "trim_whitespace=True"),
        ("min_value=-2147483648", "min_value=-1"),
        ("serializers.Serializer", "serializers.ModelSerializer"),
        ("from rest_framework import serializers", "from counterfeit import serializers"),
        ("from rest_framework import serializers", "from rest_framework import serializers as s"),
        (
            "    note = serializers.CharField",
            "    def validate(self, data):\n        return data\n    note = serializers.CharField",
        ),
    ],
)
def test_field_serializer_semantic_changes_block(tmp_path: Path, before: str, after: str) -> None:
    source = drf_field_serializer_source().replace(before, after, 1)
    assert capture_source(tmp_path, "fiber", source)["gaps"]


@pytest.mark.parametrize(
    "before,after",
    [
        ("BaseSerializer):", "Serializer):"),
        ("WidgetInput", "data"),
        ("WidgetInput", "serializer"),
        ("WidgetInput", "request"),
        ("WidgetInput", "item"),
        ("BaseSerializer):", "ModelSerializer):"),
        ("BaseSerializer):", "BaseSerializer, Other):"),
        ("return data", "return {}"),
        ("return data", "return dict(data, added=1)"),
        ("<= 2147483647", "<= 2147483648"),
        ("self.partial", "False"),
        ("serializer.is_valid()", "serializer.is_valid(raise_exception=True)"),
        ("serializer.validated_data", "request.data"),
        ("serializer.validated_data", "serializer.data"),
        ("partial=True", "partial=False"),
        ("partial=False", "partial=True"),
        ("invalid request body", "different error"),
        ("BaseSerializer, ValidationError", "BaseSerializer as Other, ValidationError"),
        ("rest_framework.serializers", "counterfeit"),
        ("class WidgetInput", "@custom\nclass WidgetInput"),
        ("class WidgetInput", "class int"),
        (
            "    def to_internal_value",
            "    def save(self):\n        pass\n    def to_internal_value",
        ),
    ],
)
def test_serializer_semantic_changes_block(tmp_path: Path, before: str, after: str) -> None:
    captured = capture_source(tmp_path, "fiber", drf_serializer_source().replace(before, after))
    assert captured["gaps"]


def test_imported_serializer_is_hashed(tmp_path: Path) -> None:
    (tmp_path / "schemas.py").write_text(serializer_source())
    source = drf_serializer_source().replace(
        serializer_source(), "from schemas import WidgetInput\n"
    )
    captured = capture_source(tmp_path, "fiber", source)
    assert captured["gaps"] == []
    assert captured["source_modules"] == ["schemas.py"]
    (tmp_path / "schemas.py").write_text(serializer_source().replace("return data", "return {}"))
    changed = capture(tmp_path, captured["configuration"])
    assert changed["gaps"]
    assert changed["source_digest"] != captured["source_digest"]


@pytest.mark.parametrize("target", TARGETS)
def test_drf_and_go_validation_agree(tmp_path: Path, target: str) -> None:
    if os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("requires qualified Go toolchain and DRF environment")
    captured = capture_source(tmp_path, target)
    assert captured["gaps"] == []
    (tmp_path / "cases.json").write_text(json.dumps(validation_cases()))
    script = """
import json
import django
from django.conf import settings
settings.configure(
    SECRET_KEY='fixture', USE_I18N=False, INSTALLED_APPS=[], ROOT_URLCONF='app',
    REST_FRAMEWORK={'UNAUTHENTICATED_USER': None},
)
django.setup()
from app import WidgetInput, create_widget, widget
replace_widget = patch_widget = widget
from rest_framework.test import APIClient
factory = APIClient()
cases = []
for partial in (False, True):
    for body in json.load(open('cases.json')):
        serializer = WidgetInput(data=body, partial=partial)
        valid = serializer.is_valid()
        cases.append({
            'body': json.dumps(body), 'partial': partial, 'valid': valid,
            'expected': serializer.validated_data if valid else None,
        })
        if not valid:
            operations = (
                [('patch', patch_widget, {'id': 1})] if partial else
                [('post', create_widget, {}), ('put', replace_widget, {'id': 1})]
            )
            for method, view, kwargs in operations:
                response = factory.generic(
                    method.upper(), '/widgets/1' if kwargs else '/widgets',
                    json.dumps(body), content_type='application/json'
                )
                response.render()
                assert response.status_code == 400, response.status_code
                assert json.loads(response.content) == {'error': 'invalid request body'}
print(json.dumps(cases))
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert_go_decoder_parity(tmp_path, captured, json.loads(result.stdout))


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("bits", [32, 64])
def test_drf_field_and_go_validation_agree(tmp_path: Path, target: str, bits: int) -> None:
    if os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("requires qualified Go toolchain and DRF environment")
    source = drf_field_serializer_source()
    models = model_source("drf")
    if bits == 64:
        source = source.replace("-2147483648", "-9223372036854775808").replace(
            "2147483647", "9223372036854775807"
        )
        models = models.replace("count = models.IntegerField()", "count = models.BigIntegerField()")
    captured = capture_source(tmp_path, target, source, models)
    assert captured["gaps"] == []
    payloads = [
        {"name": " x ", "count": "1.0", "enabled": "yes"},
        {"name": 7, "count": 1e2, "enabled": 0, "note": 12.5, "extra": 1},
        {"name": "x", "count": 1.5, "enabled": True},
        {"name": "x", "count": "1e2", "enabled": True},
        {"name": "x", "count": 1, "enabled": " true "},
        {"name": None, "count": 1, "enabled": True},
        {"name": "x" * 41, "count": 1, "enabled": True},
        {"name": "x", "count": 1, "enabled": True, "note": None},
        {
            "name": "x",
            "count": f"{2 ** (bits - 1) - 1}.0",
            "enabled": True,
        },
        {"name": "x", "count": f"{2 ** (bits - 1)}.0", "enabled": True},
        {},
    ]
    (tmp_path / "cases.json").write_text(json.dumps(payloads))
    script = """
import json
import django
from django.conf import settings
settings.configure(SECRET_KEY='fixture', USE_I18N=False, INSTALLED_APPS=[])
django.setup()
from app import WidgetInput
cases = []
for partial in (False, True):
    for body in json.load(open('cases.json')):
        serializer = WidgetInput(data=body, partial=partial)
        valid = serializer.is_valid()
        cases.append({
            'body': json.dumps(body), 'partial': partial, 'valid': valid,
            'expected': serializer.validated_data if valid else None,
        })
print(json.dumps(cases))
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert_go_decoder_parity(
        tmp_path, captured, json.loads(result.stdout), decoder="decodeWidget_drf"
    )


def test_nullable_drf_boolean_and_go_validation_agree(tmp_path: Path) -> None:
    if os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("requires qualified Go toolchain and DRF environment")
    source = drf_field_serializer_source().replace(
        "serializers.CharField(required=False, allow_null=True, allow_blank=True, "
        "trim_whitespace=False)",
        "serializers.BooleanField(required=False, allow_null=True)",
    )
    models = model_source("drf").replace(
        "models.TextField(null=True)", "models.BooleanField(null=True)"
    )
    captured = capture_source(tmp_path, "fiber", source, models)
    script = """
import json
import django
from django.conf import settings
settings.configure(SECRET_KEY='fixture', USE_I18N=False, INSTALLED_APPS=[])
django.setup()
from app import WidgetInput
cases = []
for value in ('', ' ', None, False, 'yes'):
    body = {'name': 'x', 'count': 1, 'enabled': True, 'note': value}
    serializer = WidgetInput(data=body)
    valid = serializer.is_valid()
    cases.append({
        'body': json.dumps(body), 'partial': False, 'valid': valid,
        'expected': serializer.validated_data if valid else None,
    })
print(json.dumps(cases))
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert_go_decoder_parity(
        tmp_path, captured, json.loads(result.stdout), decoder="decodeWidget_drf"
    )


@pytest.mark.skipif(
    os.getenv("SANKA_GO_TESTS") != "1" or not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN"),
    reason="requires qualified Go toolchain and explicit PostgreSQL fixture",
)
@pytest.mark.parametrize("target", TARGETS)
def test_serializer_database_lifecycle(tmp_path: Path, target: str) -> None:
    from test_golang_writes import test_write_lifecycle_and_database_effects

    test_write_lifecycle_and_database_effects(tmp_path, "drf", drf_serializer_source, target, True)

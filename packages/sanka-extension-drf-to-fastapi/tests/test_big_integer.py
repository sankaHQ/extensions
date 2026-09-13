# SPDX-License-Identifier: Apache-2.0
"""Compare builtin DRF bigint semantics with the actual generated scalar runtime."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_big_integer_scalar_parity(tmp_path: Path) -> None:
    from rest_framework import fields

    if not hasattr(fields, "BigIntegerField"):
        pytest.skip("DRF before 3.17 has no builtin BigIntegerField")
    script = r"""
import json, sys, types
sys.path[:] = json.loads(sys.argv[2])
from pathlib import Path
from django.conf import settings
settings.configure(INSTALLED_APPS=['django.contrib.contenttypes', 'rest_framework'],
    DATABASES={'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}})
import django
django.setup()
from django.db import models
from rest_framework import fields
from rest_framework.exceptions import ValidationError
from sanka_extension_drf_to_fastapi.django_fastapi import _serializer_field_ir, _field_payload
from sanka_extension_drf_to_fastapi.model import SerializerFieldIR
from sanka_extension_drf_to_fastapi.native_async import _RUNTIME, _render_models
class Item(models.Model):
    amount = models.BigIntegerField()
    class Meta: app_label = 'bigint_probe'
folder = Path(sys.argv[1])
(folder / 'sanka-manifest.json').write_text('{}')
sys.modules['sanka_store'] = types.ModuleType('sanka_store')
scope = {'__file__': str(folder / 'sanka_native.py')}
exec(compile(_RUNTIME, scope['__file__'], 'exec'), scope)
for coerce in (True, False):
    field = fields.BigIntegerField(coerce_to_string=coerce, min_value=-(2**63), max_value=2**63-1)
    captured = _serializer_field_ir('amount', field, Item)
    assert captured.supported and captured.kind == 'big_integer'
    assert captured.coerce_to_string is coerce
    assert SerializerFieldIR.from_dict(__import__('dataclasses').asdict(captured)) == captured
    spec = _field_payload(captured)
    for value in (0, 2**53+1, -(2**63), 2**63-1):
        actual = scope['_represent'](spec, value)
        expected = field.to_representation(value)
        assert actual == expected and type(actual) is type(expected)
    assert scope['_represent'](spec, None) is None
    for value in (2**53+1, str(2**53+1), '7.0', 'invalid', '9'*1001, 2**63):
        actual, errors = scope['_clean_integer'](spec, value)
        try:
            expected = field.run_validation(value)
        except ValidationError as error:
            assert errors == [str(item) for item in error.detail], (value, errors, error.detail)
        else:
            assert not errors and actual == expected
class CustomBigInteger(fields.BigIntegerField):
    def to_representation(self, value): return 'custom'
assert not _serializer_field_ir('amount', CustomBigInteger(), Item).supported
assert _serializer_field_ir('amount', fields.IntegerField(), Item).kind == 'integer'
from rest_framework.settings import api_settings
from django.test import override_settings
assert _serializer_field_ir('amount', fields.BigIntegerField(), Item).coerce_to_string == api_settings.COERCE_BIGINT_TO_STRING
for global_coerce in (True, False):
    with override_settings(REST_FRAMEWORK={'COERCE_BIGINT_TO_STRING': global_coerce}):
        assert _serializer_field_ir('amount', fields.BigIntegerField(), Item).coerce_to_string is global_coerce
manifest = {'resources': [{'db_table': 'bigint_item', 'model_class': 'Item', 'pk_attname': 'id',
    'fields': [{'name': 'id', 'kind': 'big_integer', 'read_only': True},
               {'name': 'amount', 'kind': 'big_integer'}]}]}
from tortoise import fields as tortoise_fields
namespace = {}
exec(_render_models('tortoise', manifest), namespace)
for name in ('id', 'amount'):
    assert isinstance(namespace['Item']._meta.fields_map[name], tortoise_fields.BigIntField)
from sqlalchemy.dialects import postgresql, sqlite
namespace = {}
exec(_render_models('sqlalchemy', manifest), namespace)
for column in namespace['Item'].__table__.columns:
    assert str(column.type.compile(dialect=postgresql.dialect())) == 'BIGINT'
    assert str(column.type.compile(dialect=sqlite.dialect())) == 'INTEGER'
print('bigint scalar and generated ORM parity passed')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), json.dumps(sys.path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") != "true" or sys.platform != "linux",
    reason="Real conversion acceptance runs in disposable Linux CI.",
)
@pytest.mark.parametrize("coerce_to_string", [False, True])
def test_big_integer_generated_http_and_database_parity(
    tmp_path: Path, coerce_to_string: bool
) -> None:
    import shutil

    from test_native_fastapi import FIXTURES, _generate, _run_probe

    project = tmp_path / "project"
    shutil.copytree(FIXTURES / "drf_crud_project", project)
    settings = project / "crud_config/settings.py"
    settings.write_text(
        settings.read_text().replace("models.AutoField", "models.BigAutoField")
        + f"\nREST_FRAMEWORK['COERCE_BIGINT_TO_STRING'] = {coerce_to_string!r}\n"
    )
    models = project / "inventory/models.py"
    models.write_text(
        models.read_text().replace("models.PositiveIntegerField", "models.BigIntegerField")
    )
    output = _generate(project)
    scenarios = [
        {"method": "GET", "path": "/api/gadgets/"},
        {"method": "POST", "path": "/api/gadgets/", "body": {"name": "Big", "quantity": 2**53 + 1}},
        {"method": "GET", "path": "/api/gadgets/"},
        {"method": "PATCH", "path": "/api/gadgets/1/", "body": {"quantity": str(2**63 - 1)}},
        {"method": "GET", "path": "/api/gadgets/1/"},
        {"method": "PATCH", "path": "/api/gadgets/1/", "body": {"quantity": "invalid"}},
    ]
    original = _run_probe("source", project, tmp_path / "source.sqlite3", scenarios=scenarios)
    generated = _run_probe(
        "native", project, tmp_path / "target.sqlite3", output=output, scenarios=scenarios
    )
    assert generated == original

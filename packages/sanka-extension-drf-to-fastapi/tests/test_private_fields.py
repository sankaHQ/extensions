# SPDX-License-Identifier: Apache-2.0
"""Captured private fields stay writable without entering generated responses."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_private_field_capture_and_generated_representation(tmp_path: Path) -> None:
    script = r"""
import asyncio, dataclasses, json, sys, types
sys.path[:] = json.loads(sys.argv[2])
from pathlib import Path
from django.conf import settings
settings.configure(INSTALLED_APPS=['django.contrib.contenttypes', 'rest_framework'],
    DATABASES={'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}})
import django
django.setup()
from django.db import models
from rest_framework import serializers, filters
from sanka_code_migration.drf.scan import _build_serializer_ir, _listing_support
from sanka_code_migration.drf.model import SerializerIR
from sanka_extension_drf_to_fastapi.django_fastapi import _field_payload
from sanka_extension_drf_to_fastapi.native_async import _RUNTIME
class Item(models.Model):
    label = models.CharField(max_length=100)
    secret = models.CharField(max_length=100)
    class Meta: app_label = 'private_fields'
class ItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = Item
        fields = ['id', 'label', 'secret']
        extra_kwargs = {'secret': {'write_only': True}}
ir = _build_serializer_ir(ItemSerializer, Item, name='Item', ordering=(), analyze_writes=True)
ir = SerializerIR.from_dict(dataclasses.asdict(ir))
assert ir.supported
specs = [_field_payload(field) for field in ir.fields]
folder = Path(sys.argv[1])
(folder / 'sanka-manifest.json').write_text('{}')
store = types.ModuleType('sanka_store')
async def fetch_children(spec, parent):
    return [{'id': 2, 'label': 'Child', 'secret': 'child-secret'}]
store.fetch_children = fetch_children
sys.modules['sanka_store'] = store
scope = {'__file__': str(folder / 'sanka_native.py')}
exec(compile(_RUNTIME, scope['__file__'], 'exec'), scope)
row = {'id': 1, 'label': 'Visible', 'secret': 'private'}
actual = asyncio.run(scope['_serialize_fields'](specs, row))
assert actual == dict(ItemSerializer(row).data), actual
assert actual == {'id': 1, 'label': 'Visible'}
field = next(spec for spec in specs if spec['name'] == 'secret')
assert scope['_clean_char'](field, 'accepted') == ('accepted', [])
nested = {'name': 'children', 'kind': 'nested_many', 'child': {'fields': specs}}
actual = asyncio.run(scope['_serialize_fields']([nested], row))
assert actual == {'children': [{'id': 2, 'label': 'Child'}]}, actual
nested['write_only'] = True
assert asyncio.run(scope['_serialize_fields']([nested], row)) == {}
class View:
    serializer_class = ItemSerializer
    filter_backends = [filters.OrderingFilter]
    pagination_class = None
listing, reason = _listing_support(View, ir)
assert reason is None
assert listing['ordering']['fields'] == ['id', 'label'], listing
View.ordering_fields = ['secret']
listing, reason = _listing_support(View, ir)
assert reason is None and listing['ordering']['fields'] == ['secret']
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), json.dumps(sys.path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr

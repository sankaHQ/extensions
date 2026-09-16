# SPDX-License-Identifier: Apache-2.0
"""Named-timezone datetime behavior is compared directly with DRF."""

import subprocess
import sys
from dataclasses import replace

from sanka_code_migration.drf.model import SerializerFieldIR, SerializerIR

from sanka_extension_drf_to_flask.sqlalchemy import _serializer_gaps


def test_named_timezone_validation_and_serialization_match_drf():
    script = r"""from django.conf import settings
settings.configure(USE_TZ=True, TIME_ZONE="Asia/Tokyo", INSTALLED_APPS=[])
import django
django.setup()
import datetime as dt
from zoneinfo import ZoneInfo
from rest_framework.fields import DateTimeField
from sanka_extension_drf_to_flask.sqlalchemy_runtime import _clean, _serialize

def compare(zone_name, values):
    source = DateTimeField()
    source.timezone = ZoneInfo(zone_name) if zone_name else None
    field = {
        "name": "posted",
        "column": "posted",
        "kind": "datetime",
        "timezone": zone_name,
        "allow_null": False,
        "write_only": False,
        "scalar_kind": None,
        "messages": dict(source.error_messages),
    }
    resource = {"fields": [field], "pk_column": "id"}
    for text in values:
        source_value = source.run_validation(text)
        target_value = _clean(field, text, None)
        if source_value.tzinfo is None:
            assert target_value.tzinfo is None
            assert target_value == source_value
        else:
            assert target_value.astimezone(dt.UTC) == source_value.astimezone(dt.UTC)
        assert _serialize(resource, {"posted": target_value}, None, None)["posted"] == (
            source.to_representation(source_value)
        )

compare("Asia/Tokyo", [
    "2026-09-16T12:34:56",
    "2026-09-16T12:34:56Z",
    "2026-09-16T12:34:56+02:30",
])
compare("America/New_York", [
    "2024-03-10T01:59:59",
    "2024-03-10T03:00:00",
    "2024-11-03T01:30:00",
    "2024-11-03T01:30:00-05:00",
])
compare(None, [
    "2026-09-16T12:34:56",
    "2026-09-16T12:34:56Z",
    "2026-09-16T12:34:56+02:30",
])
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_only_representable_named_timezones_are_native():
    field = SerializerFieldIR(name="posted", kind="datetime", timezone="Asia/Tokyo")
    serializer = SerializerIR(
        name="fixture.Serializer",
        model="fixture.Item",
        model_module="fixture.models",
        model_class="Item",
        object_name="Item",
        fields=(field,),
    )
    assert _serializer_gaps(serializer) == []
    unsupported = replace(serializer, fields=(replace(field, timezone="UTC+09:00"),))
    assert _serializer_gaps(unsupported) == ["datetime-timezone:posted"]

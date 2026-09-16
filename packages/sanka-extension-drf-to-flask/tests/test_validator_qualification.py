# SPDX-License-Identifier: Apache-2.0
"""Never qualify a target that drops source field validators."""

import json
import os
import subprocess
import sys

import pytest
from test_lifecycle import call
from test_native_plan import native_project


@pytest.mark.parametrize("orm", ["django", "sqlalchemy"])
@pytest.mark.parametrize(
    "validator",
    [
        "MinValueValidator(10)",
        "MinValueValidator(10, message='At least ten.')",
        "MinValueValidator(lambda: 10)",
    ],
)
def test_explicit_validator_rejection_cannot_be_lost(tmp_path, orm, validator):
    native_project(tmp_path)
    path = tmp_path / "inventory/serializers.py"
    path.write_text(
        "from django.core.validators import MinValueValidator\n"
        + path.read_text().replace(
            "serializers.IntegerField(min_value=0)",
            "serializers.IntegerField(min_value=5, validators=[" + validator + "])",
        )
    )
    script = """import json, django
django.setup()
from django.db import connection
from inventory.models import Gadget
from rest_framework.test import APIClient
with connection.schema_editor() as editor:
    editor.create_model(Gadget)
response = APIClient().post('/api/gadgets/', {'name': 'blocked', 'quantity': 1}, format='json')
print(json.dumps([response.status_code, response.json(), Gadget.objects.count()]))
"""
    source = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=os.environ
        | {
            "DJANGO_SETTINGS_MODULE": "crud_config.settings",
            "SANKA_TEST_DB": str(tmp_path / "source.db"),
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    status, body, count = json.loads(source.stdout)
    assert status == 400 and count == 0
    assert len(body["quantity"]) == 2
    config = {"settings_module": "crud_config.settings", "orm": orm}
    scan = call(tmp_path, "scan", config)
    assert scan["outcome"] == "success", scan
    plan = call(tmp_path, "plan", config)
    if orm == "sqlalchemy":
        assert plan["outcome"] == "error", plan
        assert "serializer-writes" in plan["error"]["message"]
    else:
        assert plan["data"]["needs_adaptation_routes"] > 0, plan


@pytest.mark.parametrize(
    "options, rejected",
    [
        ("min_value=Decimal('1.50')", "1.25"),
        ("max_value=Decimal('2.50')", "2.75"),
        ("normalize_output=True", None),
        ("rounding=ROUND_DOWN", None),
        ("localize=True", None),
    ],
)
def test_unrepresented_decimal_contract_blocks_native_target(tmp_path, options, rejected):
    native_project(tmp_path)
    model = tmp_path / "inventory/models.py"
    model.write_text(
        model.read_text().replace(
            "quantity = models.PositiveIntegerField(default=0)",
            "quantity = models.DecimalField(max_digits=6, decimal_places=2)",
        )
    )
    serializer = tmp_path / "inventory/serializers.py"
    serializer.write_text(
        "from decimal import Decimal, ROUND_DOWN\n"
        + serializer.read_text().replace(
            "serializers.IntegerField(min_value=0)",
            "serializers.DecimalField(max_digits=6, decimal_places=2, " + options + ")",
        )
    )
    script = """import json, django, sys
django.setup()
from django.db import connection
from inventory.models import Gadget
from rest_framework.test import APIClient
with connection.schema_editor() as editor:
    editor.create_model(Gadget)
client = APIClient()
safe = client.post('/api/gadgets/', {'name': 'safe', 'quantity': '2.00'}, format='json')
results = [safe.status_code]
if sys.argv[1] != 'None':
    bad = client.post('/api/gadgets/', {'name': 'bad', 'quantity': sys.argv[1]}, format='json')
    results.append(bad.status_code)
results.append(Gadget.objects.count())
print(json.dumps(results))
"""
    source = subprocess.run(
        [sys.executable, "-c", script, str(rejected)],
        cwd=tmp_path,
        env=os.environ
        | {
            "DJANGO_SETTINGS_MODULE": "crud_config.settings",
            "SANKA_TEST_DB": str(tmp_path / "source.db"),
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert json.loads(source.stdout) == ([201, 400, 1] if rejected else [201, 1])
    config = {"settings_module": "crud_config.settings", "orm": "sqlalchemy"}
    scan = call(tmp_path, "scan", config)
    assert scan["outcome"] == "success", scan
    plan = call(tmp_path, "plan", config)
    assert plan["outcome"] == "error", plan
    assert "serializer-writes" in plan["error"]["message"]

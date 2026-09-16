# SPDX-License-Identifier: Apache-2.0
"""Capture schema without a database connection or serializer projection."""

import json
import os
import subprocess
import sys
from pathlib import Path


def test_schema_captures_hidden_constraints_and_relations_without_database(tmp_path):
    script = r"""from django.conf import settings
settings.configure(
    INSTALLED_APPS=[],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    USE_TZ=True,
)
import django
django.setup()
from django.db import models
import uuid
from sanka_code_migration.drf.models import capture_schema
class Team(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    code = models.CharField(max_length=12, unique=True)
    class Meta:
        app_label = "fixture"
class Item(models.Model):
    team = models.ForeignKey(Team, on_delete=models.DO_NOTHING, db_column="team_key")
    hidden = models.CharField(max_length=30)
    amount = models.DecimalField(max_digits=12, decimal_places=3)
    count = models.PositiveIntegerField(default=0)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
    class Meta:
        app_label = "fixture"
        constraints = [
            models.UniqueConstraint(fields=["team", "hidden"], name="team_hidden_unique")
        ]
        indexes = [models.Index(fields=["hidden"], name="hidden_idx")]
from django.db.backends.base.base import BaseDatabaseWrapper
def no_connection(_self):
    raise AssertionError("scan connected to database")
BaseDatabaseWrapper.ensure_connection = no_connection
first = capture_schema([Item, Team])
assert first == capture_schema([Team, Item])
import json
print(json.dumps(first))
"""
    env = os.environ | {"PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    schema = json.loads(result.stdout)
    assert schema["gaps"] == []
    assert schema["dialect"] == "sqlite"
    assert schema["use_tz"] is True
    assert schema["timezone"] == "America/Chicago"
    assert schema["connection_timezone"] == "UTC"
    assert [table["name"] for table in schema["tables"]] == ["fixture_team", "fixture_item"]
    item = next(t for t in schema["tables"] if t["name"] == "fixture_item")
    columns = {c["name"]: c for c in item["columns"]}
    assert columns["hidden"]["length"] == 30
    assert columns["team_key"]["references"] == {
        "table": "fixture_team",
        "column": "id",
        "on_delete": "DO_NOTHING",
        "deferrable": True,
        "initially": "DEFERRED",
    }
    assert columns["team_key"]["index_name"].startswith("fixture_item_team_key_")
    assert columns["amount"]["precision"] == 12
    assert columns["amount"]["scale"] == 3
    assert columns["created"]["auto_now_add"] is True
    assert columns["updated"]["auto_now"] is True
    assert {"name": "team_hidden_unique", "columns": ["team_key", "hidden"]} in item[
        "unique_constraints"
    ]
    assert {"name": "hidden_idx", "columns": ["hidden"], "descending": [False]} in item["indexes"]
    assert schema["schema_hash"].startswith("sha256:")


def test_custom_schema_features_are_blocking_gaps():
    script = r"""from django.conf import settings
settings.configure(
    INSTALLED_APPS=[],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
)
import django
django.setup()
from django.db import models
from sanka_code_migration.drf.models import capture_schema
class CustomText(models.CharField):
    pass
class Item(models.Model):
    value = CustomText(max_length=10)
    class Meta:
        app_label = "fixture"
        constraints = [models.CheckConstraint(condition=models.Q(value="safe"), name="safe_only")]
class Parent(models.Model):
    class Meta: app_label = "fixture"
class Child(models.Model):
    parent = models.ForeignKey(Parent, on_delete=models.SET_DEFAULT, default=1)
    class Meta: app_label = "fixture"
class Qualified(models.Model):
    class Meta:
        app_label = "fixture"
        db_table = '"tenant"."qualified"'
schema = capture_schema([Item, Child, Qualified])
assert {g["feature"] for g in schema["gaps"]} >= {
    "custom-field", "check-constraint", "on-delete",
    "schema-qualified-or-quoted-table",
}
"""
    env = os.environ | {"PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr


def test_postgresql_foreign_key_cycles_are_blocking_gaps():
    script = r"""from django.conf import settings
settings.configure(
    INSTALLED_APPS=[],
    DATABASES={"default": {"ENGINE": "django.db.backends.postgresql", "NAME": "unused"}},
)
import django
django.setup()
from django.db import models
from sanka_code_migration.drf.models import capture_schema
class A(models.Model):
    class Meta: app_label = "fixture"
class B(models.Model):
    a = models.ForeignKey(A, on_delete=models.DO_NOTHING)
    class Meta: app_label = "fixture"
A.add_to_class("b", models.ForeignKey(B, on_delete=models.DO_NOTHING))
schema = capture_schema([B, A])
assert {g["feature"] for g in schema["gaps"]} >= {"foreign-key-cycle"}
"""
    env = os.environ | {"PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr

# SPDX-License-Identifier: Apache-2.0
"""The native database profile uses the reviewed extension lifecycle."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_lifecycle import call

FIXTURE = (
    Path(__file__).parents[2] / "sanka-extension-drf-to-fastapi/tests/fixtures/drf_crud_project"
)


def native_project(root):
    shutil.copytree(FIXTURE, root, dirs_exist_ok=True)
    with (root / "crud_config/settings.py").open("a") as out:
        out.write(
            "\nINSTALLED_APPS = ['rest_framework', 'inventory']\n"
            "\nREST_FRAMEWORK = {\n"
            "'UNAUTHENTICATED_USER': None,\n"
            "'DEFAULT_AUTHENTICATION_CLASSES': [],\n"
            "'DEFAULT_RENDERER_CLASSES': ['rest_framework.renderers.JSONRenderer'],\n"
            "'DEFAULT_PARSER_CLASSES': ['rest_framework.parsers.JSONParser'],\n}\n"
        )


@pytest.mark.parametrize("generation", ["minimal", "full", "auto"])
def test_native_profile_plan_apply_and_reviewed_configuration(tmp_path, generation):
    native_project(tmp_path)
    config = {
        "settings_module": "crud_config.settings",
        "orm": "sqlalchemy",
        "generation": generation,
    }
    scan = call(tmp_path, "scan", config)
    assert scan["outcome"] == "success", scan
    assert scan["data"]["backend_capture_hash"].startswith("sha256:")
    assert scan["data"]["database_schema"]["tables"]
    plan = call(tmp_path, "plan", config)
    assert plan["outcome"] == "success", plan
    data = plan["data"]
    assert data["orm"] == "sqlalchemy"
    assert data["native_routes"] > 0
    assert data["needs_adaptation_routes"] == 0
    manifest = json.loads(data["files"]["generated-files.json"])
    assert manifest == {
        name: hashlib.sha256(content.encode()).hexdigest()
        for name, content in data["files"].items()
        if name != "generated-files.json"
    }
    assert call(tmp_path, "plan", config)["data"]["plan_hash"] == data["plan_hash"]
    reviewed = {**config, "extension_plan_hash": data["plan_hash"]}
    assert (
        call(tmp_path, "apply", reviewed | {"orm": "django"}, "core-reviewed")["outcome"] == "error"
    )
    applied = call(tmp_path, "apply", reviewed, "core-reviewed")
    assert applied["outcome"] == "success", applied
    output = Path(applied["data"]["output"])
    assert (output / "alembic.ini").is_file()
    assert list((output / "migrations/versions").glob("*.py"))
    assert (output / "backend/models.py").is_file() is (generation == "full")
    lock_check = subprocess.run(
        ["uv", "lock", "--check", "--offline", "--project", str(output)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert lock_check.returncode == 0, lock_check.stderr
    assert call(tmp_path, "test", reviewed, "core-reviewed")["outcome"] == "success"
    generated_tests = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=output,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert generated_tests.returncode == 0, generated_tests.stderr
    assert not list(tmp_path.rglob("*.db")), "generation and boot must not write source DB"


def test_native_generation_is_portable_between_source_roots(tmp_path, monkeypatch):
    config = {"settings_module": "crud_config.settings", "orm": "sqlalchemy"}
    plans = []
    for seed, folder in enumerate(("first", "second")):
        monkeypatch.setenv("PYTHONHASHSEED", str(seed))
        monkeypatch.setenv("TZ", "UTC" if seed == 0 else "Asia/Tokyo")
        root = tmp_path / folder
        native_project(root)
        # The source bytes match even when directory insertion order and mtimes differ.
        if seed:
            sources = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            shutil.rmtree(root)
            for relative, content in sorted(sources.items(), reverse=True):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                os.utime(path, (946684800, 946684800))
        assert call(root, "scan", config)["outcome"] == "success"
        result = call(root, "plan", config)
        assert result["outcome"] == "success", result
        plans.append(result["data"])
    assert plans[0]["files"] == plans[1]["files"]
    assert plans[0]["effective_inputs_hash"] == plans[1]["effective_inputs_hash"]
    assert plans[0]["generated_content_hash"] == plans[1]["generated_content_hash"]


def test_locked_target_installs_without_the_source_framework(tmp_path):
    native_project(tmp_path)
    config = {"settings_module": "crud_config.settings", "orm": "sqlalchemy"}
    assert call(tmp_path, "scan", config)["outcome"] == "success"
    plan = call(tmp_path, "plan", config)
    assert plan["outcome"] == "success", plan
    config["extension_plan_hash"] = plan["data"]["plan_hash"]
    applied = call(tmp_path, "apply", config, "reviewed")
    assert applied["outcome"] == "success", applied
    output = Path(applied["data"]["output"])
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    installed = subprocess.run(
        ["uv", "sync", "--locked"],
        cwd=output,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert installed.returncode == 0, installed.stderr
    result = subprocess.run(
        [
            "uv",
            "run",
            "--no-sync",
            "python",
            "-c",
            "import importlib.util, unittest; "
            "assert all(importlib.util.find_spec(m) is None for m in "
            "('django','rest_framework','fastapi','sanka_code_migration')); "
            "result=unittest.TextTestRunner().run(unittest.defaultTestLoader.discover('tests')); "
            "raise SystemExit(not result.wasSuccessful())",
        ],
        cwd=output,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr

    startup = subprocess.run(
        ["uv", "run", "--no-sync", "gunicorn", "--check-config", "target_app:create_app()"],
        cwd=output,
        env=env | {"SANKA_DATABASE_URL": "sqlite:///:memory:"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert startup.returncode == 0, startup.stderr


def test_ordinary_scan_can_feed_a_later_native_plan(tmp_path):
    native_project(tmp_path)
    scan = call(tmp_path, "scan", {"settings_module": "crud_config.settings"})
    assert scan["outcome"] == "success", scan
    assert "backend_capture_hash" in scan["data"]
    plan = call(
        tmp_path,
        "plan",
        {"settings_module": "crud_config.settings", "orm": "sqlalchemy"},
    )
    assert plan["outcome"] == "success", plan


def test_native_plan_apply_and_test_reject_a_different_settings_module(tmp_path):
    native_project(tmp_path)
    config = {"settings_module": "crud_config.settings", "orm": "sqlalchemy"}
    scan = call(tmp_path, "scan", config)
    assert scan["outcome"] == "success", scan
    changed = config | {"settings_module": "crud_config.production"}
    rejected_plan = call(tmp_path, "plan", changed)
    assert rejected_plan["outcome"] == "error"
    assert "settings_module differs" in rejected_plan["error"]["message"]

    plan = call(tmp_path, "plan", config)
    assert plan["outcome"] == "success", plan
    changed["extension_plan_hash"] = plan["data"]["plan_hash"]
    rejected_apply = call(tmp_path, "apply", changed, "core-reviewed")
    assert rejected_apply["outcome"] == "error"
    assert "settings_module differs" in rejected_apply["error"]["message"]
    assert not (tmp_path / ".sanka/output/flask").exists()
    reviewed = config | {"extension_plan_hash": plan["data"]["plan_hash"]}
    applied = call(tmp_path, "apply", reviewed, "core-reviewed")
    assert applied["outcome"] == "success", applied
    rejected_test = call(tmp_path, "test", changed, "core-reviewed")
    assert rejected_test["outcome"] == "error"
    assert "settings_module differs" in rejected_test["error"]["message"]


def test_native_capture_failure_is_explicit_without_breaking_legacy_plan(tmp_path):
    native_project(tmp_path)
    (tmp_path / "unused_invalid.py").write_text("not valid Python !!!\n")
    base = {"settings_module": "crud_config.settings"}
    scan = call(tmp_path, "scan", base)
    assert scan["outcome"] == "success", scan
    assert scan["data"]["backend_capture_error"].startswith("SyntaxError:")
    native = call(tmp_path, "plan", base | {"orm": "sqlalchemy"})
    assert native["outcome"] == "error"
    assert "native backend capture unavailable" in native["error"]["message"]
    legacy = call(tmp_path, "plan", base | {"orm": "django"})
    assert legacy["outcome"] == "success", legacy


def test_delete_collector_is_a_real_service_without_repository_boilerplate(tmp_path):
    from sanka_code_migration.drf.model import FrameworkScan
    from sanka_code_migration.ir import BackendIR
    from sanka_code_migration.policy import resolve_profile

    from sanka_extension_drf_to_flask.planning import _service_operations

    native_project(tmp_path)
    config = {"settings_module": "crud_config.settings", "orm": "sqlalchemy"}
    scan = call(tmp_path, "scan", config)
    assert scan["outcome"] == "success", scan
    backend = FrameworkScan.from_dict(scan["data"]["backend_scan"])
    schema = scan["data"]["database_schema"]
    parent = schema["tables"][0]
    identifier = next(column for column in parent["columns"] if column["primary_key"])
    schema["tables"].append(
        {
            "name": "dependent",
            "model": "fixture.Dependent",
            "columns": [
                identifier,
                {
                    **identifier,
                    "name": "parent_id",
                    "attribute": "parent_id",
                    "primary_key": False,
                    "unique": False,
                    "autoincrement": False,
                    "references": {
                        "table": parent["name"],
                        "column": identifier["name"],
                        "on_delete": "CASCADE",
                    },
                },
            ],
            "indexes": [],
            "unique_constraints": [],
        }
    )
    operations = _service_operations(backend, schema)
    assert [operation.name for operation in operations] == ["delete:" + parent["name"]]
    facts = BackendIR(
        source_content_digest="sha256:" + "a" * 64,
        source_dependency_lock_digest="sha256:" + "b" * 64,
        source_settings=(),
        persistence=True,
        database_dialect="sqlite",
        route_groups=("inventory.views",),
        operations=operations,
    )
    profile = resolve_profile(facts, {"orm": "sqlalchemy", "generation": "auto"})
    assert profile.layout == "modular"
    assert profile.services == ("delete:" + parent["name"],)
    assert profile.repositories == ()


def test_reviewed_file_paths_reject_traversal(tmp_path):
    from sanka_extension_drf_to_flask.adapter import _apply

    for name in ("../escape.py", "/absolute.py", "nested/../../escape.py"):
        with pytest.raises(ValueError):
            _apply(tmp_path, tmp_path / "candidate", {name: "pass"})
    assert not (tmp_path / "candidate").exists()


def test_native_plan_rejects_tampered_capture_inventory(tmp_path):
    native_project(tmp_path)
    (tmp_path / "tasks.py").write_text("@shared_task\ndef deliver(): pass\n")
    config = {"settings_module": "crud_config.settings", "orm": "sqlalchemy"}
    scan = call(tmp_path, "scan", config)
    assert scan["outcome"] == "success", scan
    artifact = tmp_path / ".sanka/scan.json"
    payload = json.loads(artifact.read_text())
    assert payload["behavior_inventory"]
    payload["behavior_inventory"] = []
    artifact.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
    result = call(tmp_path, "plan", config)
    assert result["outcome"] == "error"
    assert "capture hash" in result["error"]["message"]


def test_native_scan_rejects_symlinked_source_directories(tmp_path):
    root = tmp_path / "project"
    native_project(root)
    external = tmp_path / "external"
    external.mkdir()
    (external / "module.py").write_text("VALUE = 1\n")
    (root / "linked_source").symlink_to(external, target_is_directory=True)
    result = call(
        root,
        "scan",
        {"settings_module": "crud_config.settings", "orm": "sqlalchemy"},
    )
    assert result["outcome"] == "error"
    assert "source directories must not be symlinks" in result["error"]["message"]


def test_postgresql_plan_boot_check_does_not_require_a_live_database(tmp_path):
    native_project(tmp_path)
    with (tmp_path / "crud_config/settings.py").open("a") as out:
        out.write(
            "\nDATABASES = {'default': {'ENGINE': 'django.db.backends.postgresql', "
            "'NAME': 'unused'}}\n"
        )
    config = {"settings_module": "crud_config.settings", "orm": "sqlalchemy"}
    scan = call(tmp_path, "scan", config)
    assert scan["outcome"] == "success", scan
    plan = call(tmp_path, "plan", config)
    assert plan["outcome"] == "success", plan
    reviewed = {**config, "extension_plan_hash": plan["data"]["plan_hash"]}
    applied = call(tmp_path, "apply", reviewed, "core-reviewed")
    assert applied["outcome"] == "success", applied
    checked = call(tmp_path, "test", reviewed, "core-reviewed")
    assert checked["outcome"] == "success", checked
    output = Path(applied["data"]["output"])
    generated = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=output,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert generated.returncode == 0, generated.stderr
    assert "skipped=2" in generated.stderr


def test_auto_timestamps_match_source_create_and_empty_patch(tmp_path):
    native_project(tmp_path)
    with (tmp_path / "crud_config/settings.py").open("a") as settings:
        settings.write("\nTIME_ZONE = 'Asia/Tokyo'\n")
    model_path = tmp_path / "inventory/models.py"
    model_path.write_text(
        model_path.read_text().replace(
            '    notes = models.CharField(max_length=120, blank=True, default="")\n',
            '    notes = models.CharField(max_length=120, blank=True, default="")\n'
            "    created = models.DateTimeField(auto_now_add=True)\n"
            "    updated = models.DateTimeField(auto_now=True)\n",
        )
    )
    serializer_path = tmp_path / "inventory/serializers.py"
    serializer_path.write_text(
        serializer_path.read_text().replace(
            'fields = ("id", "name", "quantity", "notes")',
            'fields = ("id", "name", "quantity", "notes", "created", "updated")',
        )
    )
    config = {"settings_module": "crud_config.settings", "orm": "sqlalchemy"}
    scan = call(tmp_path, "scan", config)
    assert scan["outcome"] == "success", scan
    plan = call(tmp_path, "plan", config)
    assert plan["outcome"] == "success", plan
    reviewed = {**config, "extension_plan_hash": plan["data"]["plan_hash"]}
    applied = call(tmp_path, "apply", reviewed, "core-reviewed")
    assert applied["outcome"] == "success", applied
    output = Path(applied["data"]["output"])
    source_script = """import json, time, django
django.setup()
from django.db import connection
from rest_framework.test import APIClient
from inventory.models import Gadget
with connection.schema_editor() as editor: editor.create_model(Gadget)
client = APIClient()
created = client.post('/api/gadgets/', {'name': 'clock', 'quantity': 1}, format='json')
first = Gadget.objects.get(pk=1)
times = [first.created.isoformat(), first.updated.isoformat()]
time.sleep(0.002)
updated = client.patch('/api/gadgets/1/', {}, format='json')
first.refresh_from_db()
print(json.dumps({'statuses': [created.status_code, updated.status_code],
 'created': created.json(), 'updated': updated.json(),
 'times': [*times, first.created.isoformat(), first.updated.isoformat()]}))
"""
    source = subprocess.run(
        [sys.executable, "-c", source_script],
        cwd=tmp_path,
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "crud_config.settings",
            "SANKA_TEST_DB": str(tmp_path / "source.db"),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert source.returncode == 0, source.stderr
    target_url = "sqlite:///" + str(tmp_path / "target.db")
    target_script = """import json, os, time
from alembic import command
from alembic.config import Config
from target_app import create_app
import sqlalchemy as sa
from models import TABLES
command.upgrade(Config('alembic.ini'), 'head')
app = create_app({'DATABASE_URL': os.environ['SANKA_DATABASE_URL'], 'TESTING': True})
client = app.test_client()
created = client.post('/api/gadgets/', json={'name': 'clock', 'quantity': 1})
table = TABLES['inventory_gadget']
with app.extensions['sanka_engine'].connect() as connection:
 row = connection.execute(sa.select(table).where(table.c.id == 1)).mappings().one()
 times = [row['created'].isoformat(), row['updated'].isoformat()]
time.sleep(0.002)
updated = client.patch('/api/gadgets/1/', json={})
with app.extensions['sanka_engine'].connect() as connection:
 row = connection.execute(sa.select(table).where(table.c.id == 1)).mappings().one()
print(json.dumps({'statuses': [created.status_code, updated.status_code],
 'created': created.get_json(), 'updated': updated.get_json(),
 'times': [*times, row['created'].isoformat(), row['updated'].isoformat()]}))
app.extensions['sanka_engine'].dispose()
"""
    target = subprocess.run(
        [sys.executable, "-c", target_script],
        cwd=output,
        env=os.environ | {"SANKA_DATABASE_URL": target_url},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert target.returncode == 0, target.stderr
    source_result = json.loads(source.stdout)
    target_result = json.loads(target.stdout)
    for result in (source_result, target_result):
        assert result["statuses"] == [201, 200]
        for response in (result["created"], result["updated"]):
            assert {key: response[key] for key in ("id", "name", "quantity", "notes")} == {
                "id": 1,
                "name": "clock",
                "quantity": 1,
                "notes": "",
            }
            assert datetime.fromisoformat(response["created"]).utcoffset().total_seconds() == 32400
            assert datetime.fromisoformat(response["updated"]).utcoffset().total_seconds() == 32400
        assert result["created"]["created"] == result["updated"]["created"]
        assert result["created"]["updated"] < result["updated"]["updated"]
        created_before, updated_before, created_after, updated_after = result["times"]
        assert created_before == created_after
        assert updated_before < updated_after

    def storage_utc(value):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    assert (
        abs(
            (
                storage_utc(source_result["times"][0]) - storage_utc(target_result["times"][0])
            ).total_seconds()
        )
        < 5
    )


def test_auto_timestamps_with_use_tz_false_remain_unsupported(tmp_path):
    native_project(tmp_path)
    with (tmp_path / "crud_config/settings.py").open("a") as settings:
        settings.write("\nUSE_TZ = False\nTIME_ZONE = 'Asia/Tokyo'\n")
    model_path = tmp_path / "inventory/models.py"
    model_path.write_text(
        model_path.read_text().replace(
            '    notes = models.CharField(max_length=120, blank=True, default="")\n',
            '    notes = models.CharField(max_length=120, blank=True, default="")\n'
            "    created = models.DateTimeField(auto_now_add=True)\n"
            "    updated = models.DateTimeField(auto_now=True)\n",
        )
    )
    serializer_path = tmp_path / "inventory/serializers.py"
    serializer_path.write_text(
        serializer_path.read_text().replace(
            'fields = ("id", "name", "quantity", "notes")',
            'fields = ("id", "name", "quantity", "notes", "created", "updated")',
        )
    )
    config = {"settings_module": "crud_config.settings", "orm": "sqlalchemy"}
    scan = call(tmp_path, "scan", config)
    assert scan["outcome"] == "success", scan
    serializer = scan["data"]["backend_scan"]["serializer_details"][0]
    assert serializer["supported"] is False
    rejected = call(tmp_path, "plan", config)
    assert rejected["outcome"] == "error"
    assert "serializer-writes" in rejected["error"]["message"]


def test_native_delete_uses_captured_application_cascade(tmp_path):
    native_project(tmp_path)
    model_path = tmp_path / "inventory/models.py"
    model_path.write_text(
        model_path.read_text() + "\nclass Dependent(models.Model):\n"
        "    gadget = models.ForeignKey(Gadget, on_delete=models.CASCADE)\n"
    )
    config = {
        "settings_module": "crud_config.settings",
        "orm": "sqlalchemy",
        "generation": "auto",
    }
    scan = call(tmp_path, "scan", config)
    assert scan["outcome"] == "success", scan
    plan = call(tmp_path, "plan", config)
    assert plan["outcome"] == "success", plan
    assert plan["data"]["architecture"]["layout"] == "modular"
    assert plan["data"]["architecture"]["services"] == ["delete:inventory_gadget"]
    reviewed = {**config, "extension_plan_hash": plan["data"]["plan_hash"]}
    applied = call(tmp_path, "apply", reviewed, "core-reviewed")
    assert applied["outcome"] == "success", applied
    output = Path(applied["data"]["output"])
    assert (output / "backend/sanka_native/sqlalchemy_deletion.py").is_file()
    source_script = """import json, django
django.setup()
from django.db import connection
from rest_framework.test import APIClient
from inventory.models import Gadget, Dependent
with connection.schema_editor() as editor:
 editor.create_model(Gadget)
 editor.create_model(Dependent)
client = APIClient()
created = client.post('/api/gadgets/', {'name': 'parent', 'quantity': 1}, format='json')
Dependent.objects.create(gadget_id=1)
deleted = client.delete('/api/gadgets/1/')
print(json.dumps({'statuses': [created.status_code, deleted.status_code],
 'counts': [Gadget.objects.count(), Dependent.objects.count()]}))
"""
    source = subprocess.run(
        [sys.executable, "-c", source_script],
        cwd=tmp_path,
        env=os.environ
        | {
            "DJANGO_SETTINGS_MODULE": "crud_config.settings",
            "SANKA_TEST_DB": str(tmp_path / "delete-source.db"),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert source.returncode == 0, source.stderr
    target_script = """import json, os
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from backend.models import TABLES
from target_app import create_app
command.upgrade(Config('alembic.ini'), 'head')
app = create_app({'DATABASE_URL': os.environ['SANKA_DATABASE_URL'], 'TESTING': True})
client = app.test_client()
created = client.post('/api/gadgets/', json={'name': 'parent', 'quantity': 1})
with app.extensions['sanka_engine'].begin() as connection:
 connection.execute(sa.insert(TABLES['inventory_dependent']).values(gadget_id=1))
deleted = client.delete('/api/gadgets/1/')
with app.extensions['sanka_engine'].connect() as connection:
 counts = [connection.scalar(sa.select(sa.func.count()).select_from(TABLES[name]))
           for name in ('inventory_gadget', 'inventory_dependent')]
print(json.dumps({'statuses': [created.status_code, deleted.status_code], 'counts': counts}))
app.extensions['sanka_engine'].dispose()
"""
    target = subprocess.run(
        [sys.executable, "-c", target_script],
        cwd=output,
        env=os.environ | {"SANKA_DATABASE_URL": "sqlite:///" + str(tmp_path / "delete-target.db")},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert target.returncode == 0, target.stderr
    assert (
        json.loads(source.stdout)
        == json.loads(target.stdout)
        == {
            "statuses": [201, 204],
            "counts": [0, 0],
        }
    )


def test_concurrent_unique_writes_rollback_and_release_connections(tmp_path):
    native_project(tmp_path)
    model = tmp_path / "inventory/models.py"
    model.write_text(model.read_text().replace("max_length=80", "max_length=80, unique=True"))
    config = {"settings_module": "crud_config.settings", "orm": "sqlalchemy"}
    assert call(tmp_path, "scan", config)["outcome"] == "success"
    plan = call(tmp_path, "plan", config)
    assert plan["outcome"] == "success", plan
    config["extension_plan_hash"] = plan["data"]["plan_hash"]
    applied = call(tmp_path, "apply", config, "reviewed")
    assert applied["outcome"] == "success", applied
    output = Path(applied["data"]["output"])
    script = """
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, local
from alembic import command
from alembic.config import Config
from sqlalchemy import event, text
from target_app import create_app
command.upgrade(Config('alembic.ini'), 'head')
app = create_app({'TESTING': True})
engine = app.extensions['sanka_engine']
barrier, state = Barrier(2), local()
@event.listens_for(engine, 'after_cursor_execute')
def synchronize_unique_reads(conn, cursor, statement, parameters, context, many):
    if (statement.lstrip().upper().startswith('SELECT') and 'WHERE' in statement
            and 'inventory_gadget.name' in statement and not getattr(state, 'waited', False)):
        state.waited = True
        barrier.wait(timeout=10)
def create(_):
    with app.test_client() as client:
        response = client.post('/api/gadgets/', json={'name': 'same', 'quantity': 1})
        return response.status_code, response.get_json()
with ThreadPoolExecutor(max_workers=2) as workers:
    results = list(workers.map(create, range(2)))
assert sorted(status for status, _ in results) == [201, 400], results
with engine.connect() as conn:
    assert conn.scalar(text('SELECT count(*) FROM inventory_gadget')) == 1
assert engine.pool.checkedout() == 0
engine.dispose()
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=output,
        env=os.environ | {"SANKA_DATABASE_URL": "sqlite:///concurrent.db"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_nonhidden_reviewed_output_does_not_become_source(tmp_path):
    native_project(tmp_path)
    config = {
        "settings_module": "crud_config.settings",
        "orm": "sqlalchemy",
        "output": "generated-flask",
    }
    assert call(tmp_path, "scan", config)["outcome"] == "success"
    plan = call(tmp_path, "plan", config)
    assert plan["outcome"] == "success", plan
    config["extension_plan_hash"] = plan["data"]["plan_hash"]
    assert call(tmp_path, "apply", config, "reviewed")["outcome"] == "success"
    tested = call(tmp_path, "test", config, "reviewed")
    assert tested["outcome"] == "success", tested
    models = tmp_path / "inventory/models.py"
    models.write_text(models.read_text() + "\n# Changed source\n")
    rejected = call(tmp_path, "test", config, "reviewed")
    assert rejected["outcome"] == "error", rejected
    assert "source changed" in rejected["error"]["message"]

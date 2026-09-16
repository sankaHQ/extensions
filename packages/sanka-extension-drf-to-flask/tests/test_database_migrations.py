# SPDX-License-Identifier: Apache-2.0
"""The generated schema has independent migration ownership."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from sanka_extension_drf_to_flask.database import render_database


def schema():
    column = {
        "name": "id",
        "attribute": "id",
        "kind": "integer",
        "primary_key": True,
        "nullable": False,
        "unique": True,
        "indexed": False,
        "autoincrement": True,
        "length": None,
        "precision": None,
        "scale": None,
        "positive": False,
        "default": None,
        "auto_now": False,
        "auto_now_add": False,
        "references": None,
    }
    name = {
        **column,
        "name": "name",
        "attribute": "name",
        "kind": "string",
        "length": 40,
        "primary_key": False,
        "autoincrement": False,
    }
    payload = {
        "schema_version": 1,
        "dialect": "sqlite",
        "use_tz": True,
        "timezone": "UTC",
        "connection_timezone": "UTC",
        "tables": [
            {
                "name": "item",
                "model": "fixture.Item",
                "columns": [column, name],
                "indexes": [{"name": "item_name_desc", "columns": ["name"], "descending": [True]}],
                "unique_constraints": [],
            }
        ],
        "gaps": [],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {**payload, "schema_hash": "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()}


def test_generated_migration_upgrade_and_adoption_preflight(tmp_path: Path):
    source_script = """from django.conf import settings
settings.configure(INSTALLED_APPS=[], DATABASES={'default': {
    'ENGINE': 'django.db.backends.sqlite3', 'NAME': 'source.db'}})
import django
django.setup()
from django.db import connection, models
class Item(models.Model):
    name = models.CharField(max_length=40, unique=True)
    class Meta: app_label = 'fixture'
with connection.schema_editor() as editor: editor.create_model(Item)
first = Item.objects.create(name='first').pk
Item.objects.get(pk=first).delete()
second = Item.objects.create(name='second').pk
assert (first, second) == (1, 2), (first, second)
"""
    source = subprocess.run(
        [sys.executable, "-c", source_script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert source.returncode == 0, source.stderr
    files = render_database(schema())
    assert files == render_database(schema())
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    script = """import os
from alembic.config import Config
from alembic import command
import sqlalchemy as sa
from sqlalchemy import create_engine, inspect, text
from database import EXPECTED_SCHEMA_HASH, adopt_existing, check_schema, make_engine
try:
    make_engine('postgresql://localhost/wrong_dialect')
except ValueError:
    pass
else:
    raise AssertionError('reviewed SQLite schema accepted a PostgreSQL URL')
engine = create_engine(os.environ['SANKA_DATABASE_URL'])
assert check_schema(engine), 'empty database must fail adoption'
command.upgrade(Config('alembic.ini'), 'head')
assert check_schema(engine) == []
with engine.begin() as conn:
    first = conn.scalar(text("INSERT INTO item (name) VALUES ('first') RETURNING id"))
    conn.execute(text('DELETE FROM item WHERE id = :id'), {'id': first})
    second = conn.scalar(text("INSERT INTO item (name) VALUES ('second') RETURNING id"))
    assert second == first + 1
    conn.execute(text('DELETE FROM item WHERE id = :id'), {'id': second})
with engine.begin() as conn:
    conn.execute(text("INSERT INTO item (name) VALUES ('existing')"))
assert inspect(engine).get_unique_constraints('item')
with engine.connect() as conn:
    assert conn.scalar(text('SELECT count(*) FROM item')) == 1
command.upgrade(Config('alembic.ini'), 'head')
with engine.connect() as conn:
    assert conn.scalar(text('SELECT count(*) FROM item')) == 1
with engine.begin() as conn:
    conn.execute(text('DROP INDEX item_name_desc'))
    conn.execute(text('CREATE INDEX wrong_name_and_direction ON item (name ASC)'))
assert check_schema(engine) == ['item: indexes differ']

adopt_engine = create_engine('sqlite:///' + os.environ['ADOPT_DB'])
from models import metadata
metadata.create_all(adopt_engine)
with adopt_engine.begin() as conn:
    conn.execute(text('CREATE TABLE django_migrations (id INTEGER)'))
assert check_schema(adopt_engine) == []

reuse_engine = create_engine('sqlite:///' + os.environ['REUSE_DB'])
reuse_metadata = sa.MetaData()
reuse_table = sa.Table('item', reuse_metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False, autoincrement=True),
    sa.Column('name', sa.String(40), nullable=False, unique=True))
sa.Index('item_name_desc', reuse_table.c.name.desc())
reuse_metadata.create_all(reuse_engine)
assert check_schema(reuse_engine) == ['item: autoincrement differs']
try:
    adopt_existing(adopt_engine, 'sha256:' + '0' * 64)
except ValueError:
    pass
else:
    raise AssertionError('wrong reviewed hash was accepted')
assert 'alembic_version' not in inspect(adopt_engine).get_table_names()
with adopt_engine.begin() as conn:
    conn.execute(text('CREATE TABLE unexpected (id INTEGER)'))
try:
    adopt_existing(adopt_engine, EXPECTED_SCHEMA_HASH)
except RuntimeError:
    pass
else:
    raise AssertionError('mismatched schema was stamped')
assert 'alembic_version' not in inspect(adopt_engine).get_table_names()
with adopt_engine.begin() as conn:
    conn.execute(text('DROP TABLE unexpected'))
revision = adopt_existing(adopt_engine, EXPECTED_SCHEMA_HASH)
assert adopt_existing(adopt_engine, EXPECTED_SCHEMA_HASH) == revision
with adopt_engine.connect() as conn:
    assert conn.scalar(text('SELECT version_num FROM alembic_version')) == revision

blocked_url = 'sqlite:///' + os.environ['BLOCKED_DB']
blocked_engine = create_engine(blocked_url)
with blocked_engine.begin() as conn:
    conn.execute(text('CREATE TABLE unrelated (id INTEGER PRIMARY KEY)'))
    conn.execute(text('INSERT INTO unrelated VALUES (1)'))
os.environ['SANKA_DATABASE_URL'] = blocked_url
try:
    command.upgrade(Config('alembic.ini'), 'head')
except RuntimeError as exc:
    assert 'empty database' in str(exc)
else:
    raise AssertionError('initial migration wrote beside an unrelated table')
tables = set(inspect(blocked_engine).get_table_names())
assert 'item' not in tables and 'unrelated' in tables
with blocked_engine.connect() as conn:
    assert conn.scalar(text('SELECT count(*) FROM unrelated')) == 1
    if 'alembic_version' in tables:
        assert conn.scalar(text('SELECT count(*) FROM alembic_version')) == 0
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=os.environ
        | {
            "SANKA_DATABASE_URL": "sqlite:///" + str(tmp_path / "target.db"),
            "ADOPT_DB": str(tmp_path / "adopt.db"),
            "BLOCKED_DB": str(tmp_path / "blocked.db"),
            "REUSE_DB": str(tmp_path / "reuse.db"),
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_schema_gaps_refuse_native_database_generation():
    import pytest

    invalid = schema()
    invalid["gaps"] = [{"source": "fixture.Item", "feature": "custom-field"}]
    with pytest.raises(ValueError, match="schema gaps"):
        render_database(invalid)


def test_foreign_key_dependencies_are_ordered_and_cycles_fail_closed():
    parent_id = schema()["tables"][0]["columns"][0]
    child_id = {**parent_id}
    reference = {
        **parent_id,
        "name": "parent_id",
        "attribute": "parent_id",
        "primary_key": False,
        "unique": False,
        "autoincrement": False,
        "references": {"table": "z_parent", "column": "id", "on_delete": "DO_NOTHING"},
    }
    payload = schema()
    payload["dialect"] = "postgresql"
    payload["tables"] = [
        {
            "name": "a_child",
            "model": "fixture.Child",
            "columns": [child_id, reference],
            "indexes": [],
            "unique_constraints": [],
        },
        {
            "name": "z_parent",
            "model": "fixture.Parent",
            "columns": [parent_id],
            "indexes": [],
            "unique_constraints": [],
        },
    ]
    payload.pop("schema_hash")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    payload["schema_hash"] = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
    files = render_database(payload)
    migration = files[next(name for name in files if name.startswith("migrations/versions/"))]
    assert migration.index("op.create_table('z_parent'") < migration.index(
        "op.create_table('a_child'"
    )

    payload["tables"][1]["columns"].append(
        {
            **reference,
            "name": "child_id",
            "attribute": "child_id",
            "references": {"table": "a_child", "column": "id", "on_delete": "DO_NOTHING"},
        }
    )
    payload.pop("schema_hash")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    payload["schema_hash"] = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
    import pytest

    with pytest.raises(ValueError, match="foreign-key cycle"):
        render_database(payload)
    payload["dialect"] = "sqlite"
    payload.pop("schema_hash")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    payload["schema_hash"] = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
    with pytest.raises(ValueError, match="foreign-key cycle"):
        render_database(payload)


def test_renderer_rejects_unqualified_capture_omissions():
    import pytest

    invalid = schema()
    invalid["tables"][0]["name"] = '"tenant"."item"'
    invalid.pop("schema_hash")
    encoded = json.dumps(invalid, sort_keys=True, separators=(",", ":"), allow_nan=False)
    invalid["schema_hash"] = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
    with pytest.raises(ValueError, match="schema-qualified"):
        render_database(invalid)

    invalid = schema()
    invalid["tables"][0]["columns"][1]["references"] = {
        "table": "item",
        "column": "id",
        "on_delete": "SET_DEFAULT",
    }
    invalid.pop("schema_hash")
    encoded = json.dumps(invalid, sort_keys=True, separators=(",", ":"), allow_nan=False)
    invalid["schema_hash"] = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
    with pytest.raises(ValueError, match="on_delete"):
        render_database(invalid)

    invalid = schema()
    invalid["tables"][0]["columns"][1]["references"] = {
        "table": "item",
        "column": "id",
        "on_delete": "SET_NULL",
    }
    invalid.pop("schema_hash")
    encoded = json.dumps(invalid, sort_keys=True, separators=(",", ":"), allow_nan=False)
    invalid["schema_hash"] = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
    with pytest.raises(ValueError, match="SET_NULL requires"):
        render_database(invalid)


def test_generated_auto_timestamps_follow_source_timezone_and_update_semantics():
    import types

    import sqlalchemy as sa

    payload = schema()
    base = payload["tables"][0]["columns"][1]
    payload["tables"][0]["columns"].extend(
        [
            {
                **base,
                "name": "created",
                "attribute": "created",
                "kind": "datetime",
                "length": None,
                "auto_now_add": True,
            },
            {
                **base,
                "name": "updated",
                "attribute": "updated",
                "kind": "datetime",
                "length": None,
                "auto_now": True,
            },
        ]
    )
    payload.pop("schema_hash")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    payload["schema_hash"] = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
    files = render_database(payload)
    assert "SELECT set_config('TimeZone', %s, false)" in files["database.py"]
    assert "SET TIME ZONE %s" not in files["database.py"]
    models = types.ModuleType("models")
    exec(files["models.py"], models.__dict__)
    assert models.source_now().tzinfo is not None
    table = models.TABLES["item"]
    assert table.c.created.default is not None and table.c.created.onupdate is None
    assert table.c.updated.default is not None and table.c.updated.onupdate is not None
    engine = sa.create_engine("sqlite://")
    models.metadata.create_all(engine)
    with engine.begin() as connection:
        inserted = connection.execute(sa.insert(table).values(name="first"))
        identifier = inserted.inserted_primary_key[0]
        before = (
            connection.execute(sa.select(table).where(table.c.id == identifier)).mappings().one()
        )
        connection.execute(sa.update(table).where(table.c.id == identifier).values())
        after = (
            connection.execute(sa.select(table).where(table.c.id == identifier)).mappings().one()
        )
    assert before["created"] == after["created"]
    assert after["updated"] >= before["updated"]

    payload["use_tz"] = False
    payload["timezone"] = "Asia/Tokyo"
    payload["connection_timezone"] = "Asia/Tokyo"
    payload.pop("schema_hash")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    payload["schema_hash"] = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
    files = render_database(payload)
    models = types.ModuleType("models")
    exec(files["models.py"], models.__dict__)
    from datetime import datetime
    from zoneinfo import ZoneInfo

    source_now = models.source_now()
    expected_now = datetime.now(ZoneInfo("Asia/Tokyo")).replace(tzinfo=None)
    assert source_now.tzinfo is None
    assert abs((expected_now - source_now).total_seconds()) < 2


def test_custom_integer_primary_key_does_not_gain_sqlite_sequence_ownership():
    payload = schema()
    payload["tables"][0]["columns"][0]["autoincrement"] = False
    payload.pop("schema_hash")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    payload["schema_hash"] = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
    files = render_database(payload)
    migration_name = next(name for name in files if name.startswith("migrations/versions/"))
    assert "sqlite_autoincrement" not in files["models.py"]
    assert "sqlite_autoincrement" not in files[migration_name]


def test_postgresql_serial_default_requires_matching_owned_sequence():
    import types

    files = render_database(schema())
    models = types.ModuleType("models")
    exec(files["models.py"], models.__dict__)
    sys.modules["models"] = models
    database: dict[str, object] = {}
    exec(files["database.py"], database)
    column = models.TABLES["item"].c.id

    class Connection:
        def __init__(self, matches=True):
            self.matches = matches

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def scalar(self, *_args, **_kwargs):
            return self.matches

    class Engine:
        dialect = types.SimpleNamespace(name="postgresql")

        def __init__(self, matches=True):
            self.matches = matches

        def connect(self):
            return Connection(self.matches)

    found = {
        "default": "nextval('item_id_seq'::regclass)",
        "autoincrement": True,
        "identity": None,
    }
    matches = database["_matches_autoincrement_default"]
    assert matches(Engine(), "item", column, found)
    assert not matches(Engine(False), "item", column, found)
    assert not matches(Engine(), "item", column, {**found, "default": "42"})


def test_postgresql_urls_use_the_locked_psycopg_driver():
    import types

    payload = schema()
    payload["dialect"] = "postgresql"
    payload.pop("schema_hash")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    payload["schema_hash"] = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
    files = render_database(payload)
    models = types.ModuleType("models")
    exec(files["models.py"], models.__dict__)
    sys.modules["models"] = models
    database: dict[str, object] = {}
    exec(files["database.py"], database)

    calls = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement, parameters):
            calls.append((connection.autocommit, statement, parameters))

    class Connection:
        autocommit = False

        def cursor(self):
            return Cursor()

    connection = Connection()
    database["_set_connection_timezone"](connection)
    assert connection.autocommit is False
    assert calls == [
        (
            True,
            "SELECT set_config('TimeZone', %s, false)",
            ("UTC",),
        )
    ]
    engine = database["make_engine"]("postgresql+psycopg2://localhost/unused")
    try:
        assert engine.url.drivername == "postgresql+psycopg"
    finally:
        engine.dispose()


def test_adoption_preflight_compares_foreign_keys_and_checks():
    import types

    import sqlalchemy as sa

    identifier = schema()["tables"][0]["columns"][0]
    parent_id = {
        **identifier,
        "name": "parent_id",
        "attribute": "parent_id",
        "primary_key": False,
        "unique": False,
        "autoincrement": False,
        "references": {"table": "parent", "column": "id", "on_delete": "DO_NOTHING"},
    }
    quantity = {
        **parent_id,
        "name": "quantity",
        "attribute": "quantity",
        "positive": True,
        "references": None,
    }
    payload = {
        "schema_version": 1,
        "dialect": "sqlite",
        "use_tz": True,
        "timezone": "UTC",
        "connection_timezone": "UTC",
        "tables": [
            {
                "name": "child",
                "model": "fixture.Child",
                "columns": [identifier, parent_id, quantity],
                "indexes": [],
                "unique_constraints": [],
            },
            {
                "name": "parent",
                "model": "fixture.Parent",
                "columns": [identifier],
                "indexes": [],
                "unique_constraints": [],
            },
        ],
        "gaps": [],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    payload["schema_hash"] = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
    files = render_database(payload)
    models = types.ModuleType("models")
    exec(files["models.py"], models.__dict__)
    sys.modules["models"] = models
    database: dict[str, object] = {}
    exec(files["database.py"], database)

    correct = sa.create_engine("sqlite://")
    models.metadata.create_all(correct)
    assert database["check_schema"](correct) == []

    missing = sa.create_engine("sqlite://")
    with missing.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE parent (id INTEGER NOT NULL PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE child (id INTEGER NOT NULL PRIMARY KEY, "
            "parent_id INTEGER NOT NULL, quantity INTEGER NOT NULL)"
        )
    assert database["check_schema"](missing) == [
        "child: autoincrement differs",
        "child: foreign keys differ",
        "child: check constraints differ",
        "parent: autoincrement differs",
    ]


def test_module_prefix_places_database_files_in_one_package():
    import pytest

    files = render_database(schema(), module_prefix="backend")
    assert {"backend/__init__.py", "backend/models.py", "backend/database.py"} <= files.keys()
    assert "backend/schema.json" in files
    assert "models.py" not in files and "database.py" not in files and "schema.json" not in files
    assert "from backend.models import metadata" in files["backend/database.py"]
    assert "from backend.database import make_engine" in files["migrations/env.py"]
    assert "from backend.models import metadata" in files["migrations/env.py"]
    with pytest.raises(ValueError, match="module prefix"):
        render_database(schema(), module_prefix="../backend")

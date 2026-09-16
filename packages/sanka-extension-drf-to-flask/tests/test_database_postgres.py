# SPDX-License-Identifier: Apache-2.0
"""CI PostgreSQL qualification in a newly allocated, isolated schema."""

import hashlib
import json
import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from test_database_migrations import schema

from sanka_extension_drf_to_flask.database import render_database


@pytest.mark.skipif(
    not os.environ.get("SANKA_MIGRATE_TEST_POSTGRES_DSN"),
    reason="requires the dedicated PostgreSQL test service",
)
def test_postgres_initial_schema_and_adoption_are_compatible(tmp_path):
    payload = schema()
    payload.pop("schema_hash")
    payload["dialect"] = "postgresql"
    parent = payload["tables"][0]
    parent["name"] = "z_parent"
    child_pk = dict(parent["columns"][0])
    child_fk = {
        **child_pk,
        "name": "parent_id",
        "attribute": "parent_id",
        "primary_key": False,
        "unique": False,
        "autoincrement": False,
        "references": {"table": "z_parent", "column": "id", "on_delete": "DO_NOTHING"},
    }
    payload["tables"].append(
        {
            "name": "a_child",
            "model": "fixture.Child",
            "columns": [child_pk, child_fk],
            "indexes": [],
            "unique_constraints": [],
        }
    )
    payload["schema_hash"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    for name, content in render_database(payload).items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    url = make_url(os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]).set(
        drivername="postgresql+psycopg"
    )
    isolated_schema = "sanka_flask_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text('CREATE SCHEMA "' + isolated_schema + '"'))
    try:
        scoped = url.update_query_dict({"options": "-csearch_path=" + isolated_schema})
        script = """
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from database import make_engine, check_schema, adopt_existing, EXPECTED_SCHEMA_HASH
engine = make_engine()
assert check_schema(engine)
command.upgrade(Config('alembic.ini'), 'head')
assert check_schema(engine) == [], check_schema(engine)
with engine.begin() as conn:
    key = conn.scalar(text("INSERT INTO z_parent(name) VALUES('kept') RETURNING id"))
    conn.execute(text('INSERT INTO a_child(parent_id) VALUES(:key)'), {'key': key})
adopt_existing(engine, EXPECTED_SCHEMA_HASH)
command.upgrade(Config('alembic.ini'), 'head')
with engine.connect() as conn:
    assert conn.scalar(text('SELECT count(*) FROM a_child')) == 1
with engine.begin() as conn:
    conn.execute(text('DROP INDEX item_name_desc'))
    conn.execute(text('CREATE INDEX item_name_desc ON z_parent(name ASC)'))
assert check_schema(engine) == ['z_parent: indexes differ']
engine.dispose()
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=tmp_path,
            env=os.environ | {"SANKA_DATABASE_URL": scoped.render_as_string(hide_password=False)},
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
    finally:
        with admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + isolated_schema + '" CASCADE'))
        admin.dispose()

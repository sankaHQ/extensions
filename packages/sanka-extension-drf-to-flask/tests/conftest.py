# SPDX-License-Identifier: Apache-2.0
"""Independent databases for real source/generated HTTP contract tests."""

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


@pytest.fixture(params=["sqlite", "postgresql"])
def contract_databases(request, tmp_path):
    if request.param == "sqlite":
        yield (
            {"ENGINE": "django.db.backends.sqlite3", "NAME": str(tmp_path / "source.sqlite3")},
            "sqlite:///" + str(tmp_path / "target.sqlite3"),
        )
        return
    dsn = os.environ.get("SANKA_MIGRATE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("requires the dedicated PostgreSQL test service")
    url = make_url(dsn).set(drivername="postgresql+psycopg")
    source_schema = "sanka_source_" + uuid.uuid4().hex
    target_schema = "sanka_target_" + uuid.uuid4().hex
    admin = create_engine(url)
    created = []
    try:
        for schema in (source_schema, target_schema):
            with admin.begin() as connection:
                connection.execute(text('CREATE SCHEMA "' + schema + '"'))
            created.append(schema)
        yield (
            {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": url.database,
                "USER": url.username or "",
                "PASSWORD": url.password or "",
                "HOST": url.host or "",
                "PORT": str(url.port or ""),
                "OPTIONS": {"options": "-csearch_path=" + source_schema},
            },
            url.update_query_dict({"options": "-csearch_path=" + target_schema}).render_as_string(
                hide_password=False
            ),
        )
    finally:
        try:
            for schema in reversed(created):
                with admin.begin() as connection:
                    connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        finally:
            admin.dispose()

# SPDX-License-Identifier: Apache-2.0
"""Create and retire uniquely named fixture databases on an opted-in local server."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .crud import schema_sql
from .replay import environment, run

POSTGRES_VERSION = "170011"


def connection_config() -> dict[str, Any]:
    path = Path(os.environ.get("SANKA_JAVA_POSTGRES_CONFIG", ""))
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_mode & 0o077
    ):
        raise ValueError(
            "SANKA_JAVA_POSTGRES_CONFIG must be an absolute private fixture config (0600)"
        )
    config = json.loads(path.read_text())
    if (
        set(config) != {"host", "port", "user", "password", "database"}
        or config["host"] != "127.0.0.1"
        or type(config["port"]) is not int
        or not 1024 <= config["port"] <= 65535
        or config["database"] != "postgres"
        or not isinstance(config["user"], str)
        or not isinstance(config["password"], str)
    ):
        raise ValueError(
            "fixture config must select a loopback PostgreSQL server and postgres admin database"
        )
    return dict(config)


def psql(config: dict[str, Any], sql: str, work: Path) -> str:
    executable = shutil.which("psql")
    if not executable:
        raise ValueError("psql is required for isolated fixture database creation")
    env = environment(work / "postgres-home")
    env.update(PGPASSWORD=config["password"], PGCONNECT_TIMEOUT="5", PGSSLMODE="disable")
    return run(
        [
            executable,
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            config["host"],
            "-p",
            str(config["port"]),
            "-U",
            config["user"],
            "-d",
            config["database"],
            "-Atqc",
            sql,
        ],
        work,
        env,
        timeout=30,
    ).strip()


def python_url(config: dict[str, Any]) -> str:
    return (
        "postgresql+psycopg://"
        + quote(config["user"], safe="")
        + ":"
        + quote(config["password"], safe="")
        + "@127.0.0.1:"
        + str(config["port"])
        + "/"
        + str(config["database"])
    )


def java_environment(config: dict[str, Any]) -> dict[str, str]:
    return {
        "SANKA_JAVA_DATABASE_URL": f"jdbc:postgresql://127.0.0.1:{config['port']}/{config['database']}",
        "SANKA_JAVA_DATABASE_USER": config["user"],
        "SANKA_JAVA_DATABASE_PASSWORD": config["password"],
    }


@contextmanager
def fixture_databases(work: Path, model: dict[str, Any]) -> Iterator[dict[str, dict[str, Any]]]:
    if os.environ.get("SANKA_JAVA_REPLAY") != "1":
        raise ValueError("fixture database creation requires SANKA_JAVA_REPLAY=1")
    config = connection_config()
    if psql(config, "SHOW server_version_num", work) != POSTGRES_VERSION:
        raise ValueError("the qualified fixture server must be PostgreSQL 17.11")
    created = []
    databases = {}
    try:
        for side in ("native", "candidate", "source"):
            name = "sanka_java_" + uuid.uuid4().hex
            psql(config, f"CREATE DATABASE \"{name}\" TEMPLATE template0 ENCODING 'UTF8'", work)
            created.append(name)
            database = config | {"database": name}
            psql(database, schema_sql(model), work)
            databases[side] = database
        yield databases
    finally:
        # Only databases successfully created by this invocation may be dropped.
        for name in reversed(created):
            psql(config, f'DROP DATABASE "{name}" WITH (FORCE)', work)

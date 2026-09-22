# SPDX-License-Identifier: Apache-2.0
"""Generated Go process boundaries: configuration, build, and shutdown wiring."""

from __future__ import annotations

import ast
import json
import os
import signal
import socket
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import pytest
from sanka_extension_python_to_golang.capture import TARGETS
from test_golang_reads import read_source
from test_golang_schema import generate
from test_python_to_golang import apply, source


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("database", [False, True])
def test_runtime_is_generated_and_builds(tmp_path: Path, target: str, database: bool) -> None:
    if database:
        output = generate(tmp_path, "flask", target, app_source=read_source("flask"))
    else:
        (tmp_path / "app.py").write_text(source("flask"))
        output = apply(tmp_path, "flask", target)
    command = output / "cmd/api/main.go"
    runtime_test = output / "cmd/api/main_test.go"
    assert command.is_file()
    assert runtime_test.is_file()
    text = command.read_text()
    assert "signal.NotifyContext" in text
    if target == "fiber":
        assert "GracefulContext:" in text
        assert "ShutdownTimeout:" in text
    else:
        assert "http.MaxBytesHandler" in text
        assert "ReadHeaderTimeout:" in text
        assert "server.Shutdown(shutdownCtx)" in text
    assert "DATABASE_URL is required" in text if database else "DATABASE_URL" not in text
    assert "PORT must be an integer between 1 and 65535" in text
    limits = (output / "app.go").read_text() if target == "fiber" else text
    assert "ReadTimeout:" in limits
    assert "WriteTimeout:" in limits
    assert "IdleTimeout:" in limits
    assert "BodyLimit:" in limits if target == "fiber" else "MaxBytesHandler" in limits
    if os.getenv("SANKA_GO_TESTS") == "1":
        environment = os.environ | {
            "GOTOOLCHAIN": "local",
            "GOWORK": "off",
            "GOMAXPROCS": "2",
        }
        commands = [
            ["go", "test", "-mod=readonly", "-p=2", "./..."],
            ["go", "vet", "-mod=readonly", "./..."],
            ["go", "build", "-mod=readonly", "-o", str(tmp_path / "api"), "./cmd/api"],
        ]
        for command_line in commands:
            result = subprocess.run(
                command_line,
                cwd=output,
                env=environment,
                capture_output=True,
                text=True,
                timeout=180,
            )
            assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(
    os.getenv("SANKA_GO_TESTS") != "1" or not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN"),
    reason="requires owned Go/PostgreSQL fixtures",
)
@pytest.mark.parametrize("target", TARGETS)
def test_generated_process_drains_requests_and_closes_pool(tmp_path: Path, target: str) -> None:
    import psycopg
    from psycopg import sql
    from test_golang_relational_writes import backend_source, models_source
    from test_golang_schema import schema_dsn

    source = backend_source("flask") + "\nfrom sqlalchemy import select\n"
    read = next(
        n
        for n in ast.parse(read_source("flask").replace("Widget.count", "Widget.parent_id")).body
        if isinstance(n, ast.FunctionDef)
    )
    source += ast.unparse(read)
    output = generate(
        tmp_path, "flask", target, app_source=source, model_text=models_source("flask")
    )
    binary = tmp_path / "api"
    environment = os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"}
    subprocess.run(
        ["go", "build", "-mod=readonly", "-p=2", "-o", str(binary), "./cmd/api"],
        cwd=output,
        env=environment,
        check=True,
        capture_output=True,
        timeout=180,
    )
    for values, message in [
        ({"PORT": "0", "DATABASE_URL": ""}, "PORT must be"),
        ({"PORT": "8080", "DATABASE_URL": ""}, "DATABASE_URL is required"),
        (
            {
                "PORT": "8080",
                "DATABASE_URL": "postgresql://fixture:do-not-log@127.0.0.1:1/absent?connect_timeout=1",
            },
            "database unavailable",
        ),
    ]:
        failed = subprocess.run(
            [str(binary)], env=environment | values, capture_output=True, text=True, timeout=15
        )
        assert failed.returncode != 0 and message in failed.stderr
        assert "do-not-log" not in failed.stderr
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    schema = "process_" + uuid.uuid4().hex
    parts = urlsplit(schema_dsn(dsn, schema))
    target_dsn = urlunsplit(
        parts._replace(query=urlencode([*parse_qsl(parts.query), ("application_name", schema)]))
    )
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    env = environment | {"DATABASE_URL": target_dsn, "PORT": str(port)}
    process = None
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            subprocess.run(
                ["go", "run", "-mod=readonly", "-p=2", "./cmd/migrate", "up"],
                cwd=output,
                env=env,
                capture_output=True,
                check=True,
                timeout=180,
            )
            process = subprocess.Popen(
                [str(binary)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            address = f"http://127.0.0.1:{port}/health"
            deadline = time.monotonic() + 15
            while True:
                try:
                    with urlopen(address, timeout=1) as response:
                        assert response.status == 200
                    break
                except OSError:
                    assert process.poll() is None, process.communicate()
                    assert time.monotonic() < deadline, "generated server failed to start"
                    time.sleep(0.05)
            # The generated integrity handler must preserve a unique constraint
            # under simultaneous requests, not only sequential replay.
            barrier = Barrier(2)

            def create():
                body = json.dumps({"name": "duplicate", "count": 1, "enabled": True}).encode()
                request = Request(
                    f"http://127.0.0.1:{port}/parents",
                    data=body,
                    headers={"Content-Type": "application/json"},
                )
                barrier.wait(timeout=5)
                try:
                    with urlopen(request, timeout=10) as response:
                        return response.status
                except HTTPError as error:
                    error.close()
                    return error.code

            with ThreadPoolExecutor(max_workers=2) as workers:
                attempts = [workers.submit(create) for _ in range(2)]
                assert sorted(attempt.result(timeout=15) for attempt in attempts) == [201, 409]
            assert (
                admin.execute(
                    sql.SQL("SELECT count(*) FROM {}.parents").format(sql.Identifier(schema))
                ).fetchone()[0]
                == 1
            )
            # A real database-blocked request must finish before graceful exit.
            with psycopg.connect(dsn) as blocker, ThreadPoolExecutor(max_workers=1) as workers:
                blocker.execute(
                    sql.SQL("LOCK TABLE {}.widgets IN ACCESS EXCLUSIVE MODE").format(
                        sql.Identifier(schema)
                    )
                )

                def get():
                    with urlopen(address, timeout=15) as response:
                        return response.status

                request = workers.submit(get)
                deadline = time.monotonic() + 5
                try:
                    while not admin.execute(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE application_name=%s AND wait_event_type='Lock'",
                        (schema,),
                    ).fetchone()[0]:
                        assert time.monotonic() < deadline, "request did not reach the database"
                        time.sleep(0.05)
                    process.send_signal(signal.SIGTERM)
                    time.sleep(0.2)
                    assert process.poll() is None, "shutdown abandoned an in-flight request"
                finally:
                    blocker.rollback()
                assert request.result(timeout=15) == 200
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, (stdout, stderr)
            assert (
                admin.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE application_name=%s", (schema,)
                ).fetchone()[0]
                == 0
            )
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=10)
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

# SPDX-License-Identifier: Apache-2.0
"""PostgreSQL replay tests; service checks require an explicit disposable admin URL."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from sanka_drf_replay.replay import (
    ReplayError,
    _PostgresReplay,
    _run_side,
    replay,
)


def test_postgres_requires_explicit_opt_in(tmp_path):
    with pytest.raises(ReplayError, match="requires postgres_admin_dsn_env"):
        replay(tmp_path, [], settings_module="settings", database_backend="postgresql")


def test_postgres_errors_and_timeouts_hide_secrets(tmp_path, monkeypatch):
    import importlib

    module = importlib.import_module("sanka_drf_replay.replay")
    secret = "postgresql://someone:secret@example.invalid/test"
    for outcome in (
        subprocess.CompletedProcess([], 1, "", secret),
        subprocess.TimeoutExpired("python", 1, output=secret, stderr=secret),
    ):

        def run(*args, outcome=outcome, **kwargs):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(module.subprocess, "run", run)
        with pytest.raises(ReplayError) as error:
            _run_side(
                "",
                {"database_backend": "postgresql"},
                python=Path(sys.executable),
                cwd=tmp_path,
                env={},
            )
        assert "secret" not in str(error.value)
        assert "redacted" in str(error.value)


@pytest.mark.skipif(
    not os.environ.get("SANKA_MIGRATE_TEST_POSTGRES_DSN"),
    reason="requires disposable PostgreSQL admin service",
)
def test_postgres_clone_sequences_and_cleanup(tmp_path, monkeypatch):
    import psycopg

    admin = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    owner = _PostgresReplay(
        admin,
        Path(sys.executable),
        tmp_path,
        {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("PG") and k != "SANKA_MIGRATE_TEST_POSTGRES_DSN"
        },
    )
    with psycopg.connect(admin, autocommit=True) as db:
        original_database = db.execute("SELECT current_database()").fetchone()
    try:
        seed = owner.create()
        with psycopg.connect(seed, autocommit=True) as db:
            db.execute("CREATE TABLE sample (id bigserial PRIMARY KEY, value numeric)")
            db.execute("INSERT INTO sample(value) VALUES (1.25)")
        source, candidate = owner.create(seed), owner.create(seed)
        import importlib
        from types import SimpleNamespace

        module = importlib.import_module("sanka_drf_replay.replay")
        with monkeypatch.context() as patch:
            patch.setattr(
                module.uuid,
                "uuid4",
                lambda: SimpleNamespace(hex=owner.urls[seed].removeprefix("sanka_replay_")),
            )
            with pytest.raises(ReplayError, match="collision"):
                owner.create()
        failed_owner = _PostgresReplay(admin, Path(sys.executable), tmp_path, owner.env)
        assert failed_owner.ownership_token != owner.ownership_token
        with monkeypatch.context() as patch:
            patch.setattr(
                module.uuid,
                "uuid4",
                lambda: SimpleNamespace(hex=owner.urls[seed].removeprefix("sanka_replay_")),
            )

            def uncertain_create(*args, **kwargs):
                raise ReplayError("timed out before collision result")

            patch.setattr(failed_owner, "call", uncertain_create)
            with pytest.raises(ReplayError, match="timed out"):
                failed_owner.create()
        # Even an active connection to a colliding DB must not be terminated.
        with psycopg.connect(seed, autocommit=True) as sentinel:
            with pytest.raises(ReplayError, match="ownership unconfirmed for 1"):
                failed_owner.cleanup()
            assert sentinel.execute("SELECT value FROM sample").fetchone()[0] == 1.25
        assert owner.snapshot(source, ()) == owner.snapshot(candidate, ())
        with psycopg.connect(source) as db:
            db.execute("INSERT INTO sample(value) VALUES (2.5)")
            db.rollback()
        left, right = owner.snapshot(source, ()), owner.snapshot(candidate, ())
        assert left["public.sample"] == right["public.sample"]
        assert left["public.sample_id_seq"] != right["public.sample_id_seq"]
        # A source-side failure after allocation must not keep database resources alive.
        with pytest.raises(ReplayError, match="redacted"):
            _run_side(
                "raise RuntimeError('secret credential')",
                {"database_backend": "postgresql"},
                python=Path(sys.executable),
                cwd=tmp_path,
                env=owner.env,
            )
        assert owner.snapshot(seed, ()) == right
    finally:
        owner.cleanup()
    with psycopg.connect(admin, autocommit=True) as db:
        assert db.execute("SELECT current_database()").fetchone() == original_database
        assert (
            db.execute(
                "SELECT datname FROM pg_database WHERE datname = ANY(%s)", (owner.names,)
            ).fetchall()
            == []
        )


def test_unexpected_django_alias_is_rejected_before_ready(tmp_path):
    from sanka_drf_replay.replay import _PREPARE_SCRIPT

    (tmp_path / "settings.py").write_text(
        'SECRET_KEY="test"\nINSTALLED_APPS=[]\n'
        'DATABASES={"default":{"ENGINE":"django.db.backends.sqlite3", "NAME":":memory:"}}\n'
    )
    with pytest.raises(ReplayError, match="redacted"):
        _run_side(
            _PREPARE_SCRIPT,
            {
                "database_backend": "postgresql",
                "project_root": str(tmp_path),
                "settings_module": "settings",
                "database": "postgresql://user:secret@localhost/test",
                "db_env": "SANKA_TEST_DB",
                "media_root": str(tmp_path / "media"),
            },
            python=Path(sys.executable),
            cwd=tmp_path,
            env=os.environ,
        )


def test_cleanup_attempts_remaining_databases_after_error(tmp_path, monkeypatch):
    owner = _PostgresReplay(
        "postgresql://user:admin-secret@localhost/postgres", Path(sys.executable), tmp_path, {}
    )
    owner.names = ["first", "second"]
    visited = []

    def call(action, **values):
        visited.extend(values["names"])
        raise ReplayError("failure")

    monkeypatch.setattr(owner, "call", call)
    with pytest.raises(ReplayError, match="2 isolated databases") as error:
        owner.cleanup()
    assert "database names: second, first" in str(error.value)
    assert "admin-secret" not in str(error.value)
    assert visited == ["second", "first"]


def test_flask_explicit_cookie_does_not_replace_setup_cookie(tmp_path):
    from sanka_drf_replay.replay import _CANDIDATE_SCRIPT

    (tmp_path / "settings.py").write_text("")
    (tmp_path / "app.py").write_text(
        "from flask import Flask, request, make_response\napp = Flask(__name__)\n"
        '@app.get("/set")\ndef set_cookie():\n'
        '    result=make_response("set"); result.set_cookie("session", "saved"); return result\n'
        '@app.get("/read")\ndef read(): return request.cookies.get("session", "missing")\n'
        '@app.get("/override")\ndef override():\n'
        '    assert request.cookies.get("session") == "override"\n    return "ok"\n'
    )
    result = _run_side(
        _CANDIDATE_SCRIPT,
        {
            "target": "flask",
            "project_root": str(tmp_path),
            "candidate_root": str(tmp_path),
            "settings_module": "settings",
            "database": str(tmp_path / "test.sqlite3"),
            "db_env": "SOURCE_DB",
            "candidate_db_env": "TARGET_DB",
            "entrypoint": "app.py",
            "media_root": str(tmp_path / "media"),
            "setup": [
                {"method": "GET", "path": "/set"},
                {"method": "GET", "path": "/override", "headers": {"cookie": "session=override"}},
            ],
            "request": {"method": "GET", "path": "/read"},
        },
        python=Path(sys.executable),
        cwd=tmp_path,
        env=os.environ,
    )
    import base64

    assert base64.b64decode(result["body_b64"]) == b"saved"


def test_postgres_admin_url_requires_password_before_connect(tmp_path):
    from sanka_drf_replay.replay import _POSTGRES_SCRIPT

    # Stub connect to distinguish URL validation from a failed connection attempt.
    (tmp_path / "psycopg").mkdir()
    (tmp_path / "psycopg" / "__init__.py").write_text(
        "sql = None\ndef connect(*args, **kwargs): raise AssertionError('connected')\n"
    )
    (tmp_path / "psycopg" / "conninfo.py").write_text(
        "def conninfo_to_dict(value):\n"
        "    return {'host':'localhost', 'user':'test', 'dbname':'postgres'}\n"
        "def make_conninfo(**kwargs): return ''\n"
    )
    outcome = subprocess.run(
        [sys.executable, "-c", _POSTGRES_SCRIPT],
        cwd=tmp_path,
        input='{"admin":"postgresql://test@localhost/postgres", "action":"create"}',
        text=True,
        capture_output=True,
        check=False,
    )
    assert outcome.returncode != 0
    assert "dedicated PostgreSQL URL required" in outcome.stderr
    assert "connected" not in outcome.stderr

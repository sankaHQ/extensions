# SPDX-License-Identifier: Apache-2.0
"""Shared write replay checks, including real source/Go/database acceptance."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from sanka_extension_python_to_golang.capture import SOURCES, TARGETS, capture, configuration
from sanka_extension_python_to_golang.replay import replay
from sanka_extension_python_to_golang.write_replay import (
    SOURCE_WRITES,
    normalize_bodies,
    scenarios_for,
    write_probe,
)
from test_golang_drf_validation import drf_serializer_source
from test_golang_routing import group_backend
from test_golang_schema import generate, schema_dsn
from test_golang_validation import captured_source, schema_source
from test_golang_write_parity import SCENARIOS, requires_database


def test_shared_write_contract_and_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = captured_source(tmp_path, "fastapi")
    scenarios = scenarios_for(tmp_path, captured)
    assert scenarios == scenarios_for(tmp_path, captured)
    assert any(s["method"] == "PATCH" for s in scenarios)
    assert any(s["id"].endswith("create.after") for s in scenarios)
    compile(SOURCE_WRITES, "source-probe", "exec")
    monkeypatch.delenv("SANKA_GO_TARGET_TEST_DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="resettable PostgreSQL fixtures"):
        replay(tmp_path, tmp_path / "missing", captured, "verify")
    captured["configuration"]["schema_mode"] = "adopt-existing"
    with pytest.raises(ValueError, match="schema_mode=empty"):
        replay(tmp_path, tmp_path / "missing", captured, "verify")
    captured["routes"][0]["write"]["constraints"] = {"count": {"ge": 10}}
    with pytest.raises(ValueError, match=r"explicit sanka-verify\.json"):
        scenarios_for(tmp_path, captured)
    (tmp_path / "sanka-verify.json").write_text(
        json.dumps(
            {
                "scenarios": [
                    {
                        "id": "create",
                        "method": "POST",
                        "path": "/widgets",
                        "body": {},
                        "expected_source_status": 400,
                    }
                ]
            }
        )
    )
    assert scenarios_for(tmp_path, captured)[0]["expected_status"] == 400


def test_bigint_body_normalization_keeps_errors_and_types(tmp_path: Path) -> None:
    captured = captured_source(tmp_path, "fastapi")
    observed = [
        {
            "method": "POST",
            "path": "/widgets",
            "status": 201,
            "body": {"id": 9223372036854775807, "count": 3},
        }
    ]
    normalize_bodies(observed, captured)
    assert observed[0]["body"] == {"id": "9223372036854775807", "count": 3}
    errors = [{"method": "POST", "path": "/widgets", "status": 400, "body": {"id": 2}}]
    normalize_bodies(errors, captured)
    assert errors[0]["body"]["id"] == 2


@pytest.mark.skipif(os.getenv("SANKA_GO_TESTS") != "1", reason="requires qualified Go toolchain")
@pytest.mark.parametrize("target", TARGETS)
def test_shared_probe_compiles(tmp_path: Path, target: str) -> None:
    output = generate(tmp_path, "fastapi", target, app_source=schema_source("fastapi"))
    captured = capture(
        tmp_path,
        configuration(
            {"source_framework": "fastapi", "target_framework": target, "database_layer": "pgx"}
        ),
    )
    (output / "sanka_contract_probe_test.go").write_text(write_probe(captured))
    result = subprocess.run(
        ["go", "test", "-mod=readonly", "-p=2", "-run", "^$", "./..."],
        cwd=output,
        env=os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@requires_database
@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
def test_public_write_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, framework: str, target: str
) -> None:
    import psycopg
    from psycopg import sql

    source = drf_serializer_source() if framework == "drf" else schema_source(framework)
    output = generate(tmp_path, framework, target, app_source=group_backend(source, framework))
    captured = capture(
        tmp_path,
        configuration(
            {"source_framework": framework, "target_framework": target, "database_layer": "pgx"}
        ),
    )
    cases = [
        {
            "id": f"step{index}",
            "method": case["method"],
            "path": case["path"],
            "expected_status": case["status"],
            **({"body": case["body"]} if "body" in case else {}),
        }
        for index, case in enumerate(SCENARIOS)
    ]
    (tmp_path / "sanka-verify.json").write_text(
        json.dumps({"schema": "sanka.http-scenarios/v1", "scenarios": cases})
    )
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    created = []
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            for _ in range(2):
                name = "go_shared_" + uuid.uuid4().hex
                admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
                created.append(name)
            source_url, target_url = [schema_dsn(dsn, name) for name in created]
            if framework != "drf":
                source_url = source_url.replace("postgresql://", "postgresql+psycopg://", 1)
            monkeypatch.setenv("SANKA_GO_SOURCE_TEST_DATABASE_URL", source_url)
            monkeypatch.setenv("SANKA_GO_TARGET_TEST_DATABASE_URL", target_url)
            report = replay(tmp_path, output, captured, "verify")
            assert report["ok"], report["steps"]
            assert report["candidate"] == report["source"]
            assert report["candidate"][-1]["sequences"]["widgets"][0] == "2"
            assert report["complete_backend"] is False
            # Replaying resets both populated fixtures, including identity state.
            if framework == "fastapi" and target == "fiber":
                separator = "&" if "?" in target_url else "?"
                alias = (
                    target_url.replace("postgresql://", "postgresql+psycopg://", 1)
                    + separator
                    + "application_name=source-alias"
                )
                monkeypatch.setenv("SANKA_GO_SOURCE_TEST_DATABASE_URL", alias)
                with pytest.raises(ValueError, match="same database schema"):
                    replay(tmp_path, output, captured, "verify")
                with psycopg.connect(target_url) as connection:
                    assert connection.execute("SELECT count(*) FROM widgets").fetchone()[0] == 1
                monkeypatch.setenv("SANKA_GO_SOURCE_TEST_DATABASE_URL", source_url)
                repeated = replay(tmp_path, output, captured, "verify")
                assert repeated["candidate"] == report["candidate"]
                # Keep HTTP output identical while changing the persisted row.
                app = output / "app.go"
                app.write_text(
                    app.read_text().replace(
                        "return saved, err",
                        "if err == nil { _, err = pool.Exec(ctx, "
                        "\"UPDATE widgets SET note='tampered'\") }; return saved, err",
                    )
                )
                changed = replay(tmp_path, output, captured, "verify")
                assert not changed["ok"]
                assert any(
                    "$.tables" in problem
                    for step in changed["steps"]
                    for problem in step["problems"]
                )
        finally:
            for name in created:
                admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))

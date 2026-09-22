# SPDX-License-Identifier: Apache-2.0
"""Qualified single-table lookups and writes: capture by exact idiom, PostgreSQL parity."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from test_rust_database import DATABASE, database_request, needs_postgres, schema_dsn
from test_typescript_to_rust import needs_node, needs_rust

from sanka_extension_typescript_to_rust.adapter import handle
from sanka_extension_typescript_to_rust.capture import canonical, capture, configuration
from sanka_extension_typescript_to_rust.replay import operations, scenario_models
from sanka_extension_typescript_to_rust.writes import handler_source
from sanka_http_replay import default_scenarios

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pg-writes"
TOOLS = Path(__file__).resolve().parent / "node-tools"


def project(root: Path, app: str | None = None) -> Path:
    for name in ("schema.sql", "package.json", "src/models.ts", "src/app.ts"):
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURE / name, destination)
    if app is not None:
        (root / "src" / "app.ts").write_text(app)
    return root


def source_text() -> str:
    return (FIXTURE / "src" / "app.ts").read_text()


def gaps(root: Path) -> list[str]:
    return list(capture(root, configuration(dict(DATABASE)))["gaps"])


def apply(root: Path) -> Path:
    req = database_request(root)
    planned = handle(req)
    assert planned.outcome == "success", planned.error
    assert planned.data["capture"]["gaps"] == []
    result = handle(
        dataclasses.replace(
            req,
            command="apply",
            reviewed_plan_hash="runtime-reviewed-plan",
            configuration=req.configuration | {"extension_plan_hash": planned.data["plan_hash"]},
        )
    )
    assert result.outcome == "success", result.error
    return Path(str(result.data["output"]))


@needs_node
def test_lookup_and_write_capture(tmp_path: Path) -> None:
    project(tmp_path)
    plan = handle(database_request(tmp_path))
    assert plan.outcome == "success", plan.error
    captured = plan.data["capture"]
    assert captured["gaps"] == []
    assert [(route["method"], route["path"], route["status"]) for route in captured["routes"]] == [
        ("GET", "/widgets", 200),
        ("POST", "/widgets", 201),
        ("DELETE", "/widgets/:id", 204),
        ("GET", "/widgets/:id", 200),
        ("PATCH", "/widgets/:id", 200),
        ("PUT", "/widgets/:id", 200),
    ]
    writes = [route["write"] for route in captured["routes"] if "write" in route]
    assert [write["operation"] for write in writes] == ["create", "delete", "update", "replace"]
    assert all(write["model"] == "Widget" for write in writes)
    assert writes[0] == {
        "model": "Widget",
        "table": "widgets",
        "operation": "create",
        "conflict": True,
    }
    assert captured["routes"][3]["lookup"] == {"model": "Widget", "table": "widgets", "param": "id"}
    assert "qualified single-table lookups and writes" in captured["scope"]
    library = plan.data["files"]["src/lib.rs"]
    assert 'router.route("/widgets", get(route_0).post(route_1));' in library
    assert '"/widgets/{id}",' in library
    assert "get(route_3).put(route_5).patch(route_4).delete(route_2)" in library
    for helper in ("fn route_id(", "fn only_fields(", "fn integer_field(", "fn write_error("):
        assert helper in library
    assert 'Some("23505")' in library
    assert "StatusCode::NO_CONTENT.into_response()" in library
    assert '"note" = CASE WHEN $8 THEN $9 ELSE "note" END' in library
    # Replay follows the shared scenario contract; it needs explicit fixture databases.
    apply(tmp_path)
    for name in ("SANKA_RUST_TARGET_TEST_DATABASE_URL", "SANKA_RUST_SOURCE_TEST_DATABASE_URL"):
        os.environ.pop(name, None)
    response = handle(database_request(tmp_path, "test"))
    assert response.outcome == "error"
    assert response.error is not None
    assert "SANKA_RUST_TARGET_TEST_DATABASE_URL" in response.error.message
    scenarios = default_scenarios(operations(captured), scenario_models(captured))
    assert [scenario["id"] for scenario in scenarios][:5] == [
        "Widget.create.missing",
        "Widget.create.wrong-type",
        "Widget.create.unknown-key",
        "Widget.create.first",
        "Widget.create.duplicate",
    ]
    assert scenarios[-1] == {
        "id": "get.widgets",
        "method": "GET",
        "path": "/widgets",
        "headers": {},
        "expected_status": 200,
    }


@needs_node
def test_write_idioms_are_exact(tmp_path: Path) -> None:
    text = source_text()
    variants = {
        "missing json middleware": text.replace("app.use(express.json());\n", ""),
        "different error message": text.replace(
            'return res.status(400).json({ error: "invalid request body" });',
            'return res.status(400).json({ error: "bad body" });',
            1,
        ),
        "synchronous handler": text.replace(
            'app.delete("/widgets/:id", async (req, res) => {',
            'app.delete("/widgets/:id", (req, res) => {',
        ),
        "post with parameter": text.replace('app.post("/widgets",', 'app.post("/widgets/:id",'),
        "patch without parameter": text.replace(
            'app.patch("/widgets/:id",', 'app.patch("/widgets",'
        ),
        "extra statement": text.replace(
            "  return res.status(204).end();\n",
            "  console.log(id);\n  return res.status(204).end();\n",
        ),
        "renamed local": text[: text.index('app.delete("')]
        + text[text.index('app.delete("') :]
        .replace("const id = Number(req.params.id);", "const key = Number(req.params.id);", 1)
        .replace("if (!Number.isInteger(id)) {", "if (!Number.isInteger(key)) {", 1)
        .replace("[id]);", "[key]);", 1),
        "wider limit check": text.replace(
            "body.count > 2147483647", "body.count > 9007199254740991"
        ),
    }
    for name, variant in variants.items():
        assert variant != text, name
        project(tmp_path, app=variant)
        assert gaps(tmp_path), name
    # Cosmetic differences are not deviations: quotes, parentheses, type assertions.
    cosmetic = (
        text.replace(
            '"SELECT id, name, count, enabled, note FROM widgets WHERE id = $1"',
            "'select  id,name,count,enabled,note from widgets where id=$1'",
        )
        .replace("(error as { code?: string }).code", "(error as any).code")
        .replace("return res.status(204).end();", "return (res.status(204)).end();")
    )
    project(tmp_path, app=cosmetic)
    assert gaps(tmp_path) == []
    # Models with request fields outside the qualified types cannot be written.
    project(tmp_path)
    (tmp_path / "schema.sql").write_text(
        (FIXTURE / "schema.sql")
        .read_text()
        .replace("count integer NOT NULL", "count bigint NOT NULL")
    )
    found = gaps(tmp_path)
    assert found and any("unsupported request fields" in gap for gap in found)
    # A function-expression handler with the same body is the same idiom.
    functional = text.replace(
        'app.delete("/widgets/:id", async (req, res) => {',
        'app.delete("/widgets/:id", async function (req, res) {',
    )
    project(tmp_path, app=functional)
    assert gaps(tmp_path) == []


def test_canonical_idioms_render_every_operation() -> None:
    model = {
        "name": "Widget",
        "table": "widgets",
        "fields": [
            {
                "name": "id",
                "rust_type": "i32",
                "nullable": False,
                "primary_key": True,
                "unique": False,
                "auto": True,
            },
            {
                "name": "name",
                "rust_type": "String",
                "nullable": False,
                "primary_key": False,
                "unique": False,
                "auto": False,
            },
            {
                "name": "note",
                "rust_type": "String",
                "nullable": True,
                "primary_key": False,
                "unique": False,
                "auto": False,
            },
        ],
    }
    rendered = {
        operation: handler_source(operation, model, "id", True)
        for operation in ("lookup", "create", "update", "replace", "delete")
    }
    assert "try {" not in rendered["create"], "no unique column, no conflict branch"
    assert (
        "INSERT INTO widgets (name, note) VALUES ($1, $2) RETURNING id, name, note"
        in rendered["create"]
    )
    assert "CASE WHEN $2 THEN $3 ELSE name END" in rendered["update"]
    assert "DELETE FROM widgets WHERE id = $1 RETURNING id" in rendered["delete"]
    assert "status(204).end()" in rendered["delete"]


@needs_node
@needs_rust
@needs_postgres
def test_write_lifecycle(tmp_path: Path, node_tools: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import psycopg
    from psycopg import sql

    project(tmp_path)
    output = apply(tmp_path)
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    schemas = ["rust_write_" + uuid.uuid4().hex for _ in range(2)]
    source_url, target_url = (schema_dsn(dsn, schema) for schema in schemas)
    monkeypatch.setenv("SANKA_RUST_SOURCE_TEST_DATABASE_URL", source_url)
    monkeypatch.setenv("SANKA_RUST_TARGET_TEST_DATABASE_URL", target_url)
    captured = handle(database_request(tmp_path)).data["capture"]
    expected = default_scenarios(operations(captured), scenario_models(captured))
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            for schema in schemas:
                admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            # The source fixture database only needs to exist: write contracts reset the
            # captured tables to the baseline on both sides before the sequence runs.
            with psycopg.connect(source_url, autocommit=True) as connection:
                connection.execute((FIXTURE / "schema.sql").read_text())
                connection.execute(
                    "INSERT INTO widgets (name, count, enabled) VALUES ('stale', 1, true)"
                )
            verified = handle(database_request(tmp_path, "verify"))
            assert verified.outcome == "success", verified.error
            report = verified.data
            assert report["scenario_origin"] == "default"
            assert report["scenarios"] == expected
            assert [item["status"] for item in report["candidate"]] == [
                scenario["expected_status"] for scenario in expected
            ]
            assert canonical(report["candidate"]) == canonical(report["source"])
            by_id = {item["id"]: item for item in report["source"]}
            assert by_id["Widget.create.first"]["body"] == {
                "id": 1,
                "name": "alpha",
                "count": 7,
                "enabled": True,
                "note": None,
            }
            assert by_id["Widget.create.duplicate"]["body"] == {"error": "conflict"}
            assert by_id["Widget.update.clear"]["body"]["note"] is None
            assert by_id["Widget.delete.first"]["body"] is None
            assert by_id["Widget.delete.first"]["media_type"] == ""
            assert by_id["Widget.delete.first"]["tables"] == {"widgets": []}
            assert by_id["Widget.create.after"]["body"]["id"] == 3
            assert by_id["Widget.create.after"]["sequences"] == {"widgets": ["3", True]}
            assert by_id["get.widgets"]["body"] == by_id["get.widgets"]["tables"]["widgets"]
            tested = handle(database_request(tmp_path, "test"))
            assert tested.outcome == "success", tested.error
            assert tested.data["comparison"]["ok"] is True
            fmt = subprocess.run(
                ["cargo", "fmt", "--check"], cwd=output, capture_output=True, text=True, check=False
            )
            assert fmt.returncode == 0, fmt.stdout + fmt.stderr
            # Declared scenarios in the hosted spelling replace the defaults.
            (tmp_path / "sanka-verify.json").write_text(
                json.dumps(
                    {
                        "scenarios": [
                            {
                                "id": "seed",
                                "method": "POST",
                                "path": "/widgets",
                                "body": {"name": "declared", "count": 1, "enabled": True},
                                "expected_source_status": 201,
                            },
                            {
                                "id": "list",
                                "method": "GET",
                                "path": "/widgets",
                                "expected_source_status": 200,
                            },
                        ]
                    }
                )
            )
            declared = handle(database_request(tmp_path, "verify"))
            assert declared.outcome == "success", declared.error
            assert declared.data["scenario_origin"] == "declared"
            assert [item["id"] for item in declared.data["source"]] == ["seed", "list"]
            assert declared.data["source"][1]["body"] == [
                {"id": 1, "name": "declared", "count": 1, "enabled": True, "note": None}
            ]
            (tmp_path / "sanka-verify.json").unlink()
            # Matching responses cannot hide a different database: the candidate keeps
            # answering the same JSON while an extra statement changes the stored rows.
            library = output / "src" / "lib.rs"
            text = library.read_text()
            marker = "    match inserted {\n"
            assert text.count(marker) == 1
            library.write_text(
                text.replace(
                    marker,
                    "    let _ = sqlx::query("
                    '"UPDATE \\"widgets\\" SET \\"note\\" = \'tampered\'")\n'
                    "        .execute(&pool)\n        .await;\n" + marker,
                )
            )
            tampered = handle(database_request(tmp_path, "verify"))
            assert tampered.outcome == "error"
            assert tampered.error is not None
            assert tampered.error.code == "SANKA_EXTENSION_PARITY_FAILED"
            report = json.loads((tmp_path / ".sanka/rust/verify.json").read_text())
            assert report["ok"] is False
            problems = [step for step in report["comparison"]["steps"] if step["problems"]]
            assert problems and "$.tables.widgets" in problems[0]["problems"][0]
        finally:
            for schema in schemas:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )

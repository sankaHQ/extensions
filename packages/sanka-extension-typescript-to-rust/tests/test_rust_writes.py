# SPDX-License-Identifier: Apache-2.0
"""Qualified single-table lookups and writes: capture by exact idiom, PostgreSQL parity."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest
from test_rust_database import DATABASE, database_request, needs_postgres, schema_dsn
from test_typescript_to_rust import needs_node, needs_rust

from sanka_extension_typescript_to_rust.adapter import handle
from sanka_extension_typescript_to_rust.capture import canonical, capture, configuration
from sanka_extension_typescript_to_rust.writes import handler_source
from sanka_ts_capture import node_executable, transpile_sources

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pg-writes"
TOOLS = Path(__file__).resolve().parent / "node-tools"
WIDGET_COLUMNS = "id, name, count, enabled, note"
SEQUENCE_SQL = "SELECT last_value::integer AS value, is_called FROM widgets_id_seq"
SCENARIOS: list[dict[str, object]] = [
    {"method": "POST", "path": "/widgets", "body": {"name": "missing"}, "status": 400},
    {
        "method": "POST",
        "path": "/widgets",
        "body": {"name": "wrong", "count": True, "enabled": False},
        "status": 400,
    },
    {
        "method": "POST",
        "path": "/widgets",
        "body": {"name": "alpha", "count": 7, "enabled": True, "note": None},
        "status": 201,
    },
    {
        "method": "POST",
        "path": "/widgets",
        "body": {"name": "alpha", "count": 1, "enabled": True},
        "status": 409,
    },
    {"method": "GET", "path": "/widgets/1", "status": 200},
    {"method": "GET", "path": "/widgets/abc", "status": 404},
    {"method": "GET", "path": "/widgets/999", "status": 404},
    {
        "method": "PATCH",
        "path": "/widgets/1",
        "body": {"count": 0, "enabled": False, "note": ""},
        "status": 200,
    },
    {"method": "PATCH", "path": "/widgets/1", "body": {}, "status": 200},
    {"method": "PATCH", "path": "/widgets/1", "body": {"note": None}, "status": 200},
    {"method": "PATCH", "path": "/widgets/1", "body": {"name": None}, "status": 400},
    {"method": "PATCH", "path": "/widgets/1", "body": {"extra": 1}, "status": 400},
    {"method": "PATCH", "path": "/widgets/999", "body": {"count": 1}, "status": 404},
    {"method": "PUT", "path": "/widgets/1", "body": {"name": "missing"}, "status": 400},
    {
        "method": "PUT",
        "path": "/widgets/1",
        "body": {"name": "beta", "count": 9, "enabled": False},
        "status": 200,
    },
    {
        "method": "PUT",
        "path": "/widgets/999",
        "body": {"name": "missing", "count": 1, "enabled": True},
        "status": 404,
    },
    {"method": "DELETE", "path": "/widgets/1", "status": 204},
    {"method": "DELETE", "path": "/widgets/1", "status": 404},
    {
        "method": "POST",
        "path": "/widgets",
        "body": {"name": "next", "count": -2147483648, "enabled": False},
        "status": 201,
    },
    {
        "method": "POST",
        "path": "/widgets",
        "body": {"name": "overflow", "count": 2147483648, "enabled": True},
        "status": 400,
    },
    {
        "method": "POST",
        "path": "/widgets",
        "body": {"name": "float", "count": 7.0, "enabled": True, "note": "日本語 <tag>&"},
        "status": 201,
    },
    {"method": "PATCH", "path": "/widgets/3", "body": {"name": "float"}, "status": 409},
    {"method": "GET", "path": "/widgets", "status": 200},
]

# Test-owned Express runner: serves the transpiled source on a Unix socket and records
# every response together with the table rows and identity sequence afterwards.
SOURCE_RUNNER = """// SPDX-License-Identifier: Apache-2.0
"use strict";
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { Pool } = require("pg");

const [appPath, casesPath, destination] = process.argv.slice(2);
const loaded = require(path.resolve(appPath));
const app = loaded && loaded.__esModule && loaded.default ? loaded.default : loaded;
const cases = JSON.parse(fs.readFileSync(casesPath, "utf8"));
const socketPath = path.join(process.cwd(), "s.sock");
const pool = new Pool({ connectionString: process.env.DATABASE_URL });

function exchange(scenario) {
  return new Promise((resolve, reject) => {
    const payload = "body" in scenario ? JSON.stringify(scenario.body) : null;
    const headers = payload === null
      ? {}
      : { "content-type": "application/json", "content-length": Buffer.byteLength(payload) };
    const request = http.request(
      { socketPath, path: scenario.path, method: scenario.method, headers },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () => {
          const text = Buffer.concat(chunks).toString("utf8");
          let body = null;
          if (text.length) {
            try {
              body = JSON.parse(text);
            } catch {
              reject(new Error(`non-JSON response for ${scenario.method} ${scenario.path}`));
              return;
            }
          }
          resolve({
            status: response.statusCode,
            media_type: String(response.headers["content-type"] || "").split(";")[0],
            body,
          });
        });
      }
    );
    request.on("error", reject);
    if (payload !== null) request.write(payload);
    request.end();
  });
}

const server = http.createServer(app);
server.listen(socketPath, async () => {
  let code = 0;
  try {
    const observed = [];
    for (const scenario of cases) {
      const result = await exchange(scenario);
      const rows = (await pool.query("SELECT @COLUMNS@ FROM widgets ORDER BY id")).rows;
      const sequence = (await pool.query("@SEQUENCE@")).rows[0];
      observed.push({
        method: scenario.method,
        path: scenario.path,
        ...result,
        rows,
        sequence: [sequence.value, sequence.is_called],
      });
    }
    fs.writeFileSync(destination, JSON.stringify(observed));
  } catch (error) {
    console.error(String((error && error.message) || error));
    code = 1;
  } finally {
    await pool.end();
    server.close(() => process.exit(code));
  }
});
""".replace("@COLUMNS@", WIDGET_COLUMNS).replace("@SEQUENCE@", SEQUENCE_SQL)

# Test-owned axum probe: the same scenarios through tower::ServiceExt::oneshot.
TARGET_PROBE = """// SPDX-License-Identifier: Apache-2.0
use axum::body::{Body, to_bytes};
use axum::http::Request;
use serde_json::{Value, json};
use sqlx::Row;
use tower::ServiceExt;

#[tokio::test]
async fn sanka_write_parity() {
    let url = std::env::var("DATABASE_URL").expect("DATABASE_URL must be set");
    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(1)
        .connect(&url)
        .await
        .expect("connect to DATABASE_URL");
    let raw = std::fs::read_to_string("write-cases.json").expect("cases");
    let cases: Vec<Value> = serde_json::from_str(&raw).expect("cases json");
    let mut observed = Vec::new();
    for case in cases {
        let method = case["method"].as_str().expect("method").to_string();
        let path = case["path"].as_str().expect("path").to_string();
        let builder = Request::builder().method(method.as_str()).uri(&path);
        let request = match case.get("body") {
            Some(value) => builder
                .header("content-type", "application/json")
                .body(Body::from(serde_json::to_vec(value).expect("encode body"))),
            None => builder.body(Body::empty()),
        }
        .expect("request");
        let response = migrated_backend::app(pool.clone())
            .oneshot(request)
            .await
            .expect("response");
        let status = response.status().as_u16();
        let media_type = response
            .headers()
            .get("content-type")
            .and_then(|value| value.to_str().ok())
            .unwrap_or("")
            .split(';')
            .next()
            .unwrap_or("")
            .to_string();
        let bytes = to_bytes(response.into_body(), 1_048_576).await.expect("body");
        let body: Value = if bytes.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&bytes).expect("JSON body")
        };
        @TAMPER@
        let rows = sqlx::query_as::<_, migrated_backend::models::Widget>(
            "SELECT @COLUMNS@ FROM widgets ORDER BY id",
        )
        .fetch_all(&pool)
        .await
        .expect("rows");
        let sequence = sqlx::query("@SEQUENCE@")
            .fetch_one(&pool)
            .await
            .expect("sequence");
        let value: i32 = sequence.get("value");
        let called: bool = sequence.get("is_called");
        observed.push(json!({
            "method": method,
            "path": path,
            "status": status,
            "media_type": media_type,
            "body": body,
            "rows": rows,
            "sequence": [value, called],
        }));
    }
    let encoded = serde_json::to_vec(&observed).expect("encode");
    std::fs::write("write-observed.json", encoded).expect("write");
}
""".replace("@COLUMNS@", WIDGET_COLUMNS).replace("@SEQUENCE@", SEQUENCE_SQL)
TAMPER = (
    "if status == 201 { sqlx::query(\"UPDATE widgets SET note = 'tampered'\")"
    '.execute(&pool).await.expect("tamper"); }'
)


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
    # Public replay stays refused for write contracts, exactly like the Go extension.
    apply(tmp_path)
    for command in ("test", "verify"):
        response = handle(database_request(tmp_path, command))
        assert response.outcome == "error"
        assert response.error is not None
        assert "shared HTTP scenario adapter" in response.error.message


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
    environment = os.environ | {"CARGO_TERM_COLOR": "never"}
    environment.setdefault("CARGO_TARGET_DIR", str(output.parent / "rust-target"))
    cases = [{key: value for key, value in case.items() if key != "status"} for case in SCENARIOS]
    node = node_executable()

    def cargo(arguments: list[str], cwd: Path, url: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["cargo", *arguments],
            cwd=cwd,
            env=environment | {"DATABASE_URL": url},
            capture_output=True,
            text=True,
            timeout=1200,
            check=False,
        )

    def target_run(*, tamper: bool) -> list[dict[str, object]]:
        with tempfile.TemporaryDirectory(prefix="sanka-write-probe-") as temporary:
            candidate = Path(temporary) / "candidate"
            shutil.copytree(output, candidate)
            (candidate / "tests").mkdir(exist_ok=True)
            (candidate / "tests" / "sanka_write_probe.rs").write_text(
                TARGET_PROBE.replace("@TAMPER@", TAMPER if tamper else "")
            )
            (candidate / "write-cases.json").write_text(json.dumps(cases))
            run = cargo(
                ["test", "--locked", "--quiet", "--test", "sanka_write_probe"],
                candidate,
                target_url,
            )
            assert run.returncode == 0, run.stdout + run.stderr
            observed: list[dict[str, object]] = json.loads(
                (candidate / "write-observed.json").read_text()
            )
            return observed

    def source_run() -> list[dict[str, object]]:
        with tempfile.TemporaryDirectory(prefix="sanka-write-source-") as temporary:
            directory = Path(temporary)
            transpiled = transpile_sources({"src/app.ts": source_text()}, node=node)
            (directory / "app.js").write_text(transpiled["src/app.ts"])
            (directory / "run.js").write_text(SOURCE_RUNNER)
            (directory / "cases.json").write_text(json.dumps(cases))
            run = subprocess.run(
                [node, "run.js", "app.js", "cases.json", "observed.json"],
                cwd=directory,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "NODE_PATH": str(node_tools),
                    "DATABASE_URL": source_url,
                },
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            assert run.returncode == 0, run.stdout + run.stderr
            observed: list[dict[str, object]] = json.loads(
                (directory / "observed.json").read_text()
            )
            return observed

    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            for schema in schemas:
                admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            with psycopg.connect(source_url, autocommit=True) as connection:
                connection.execute((FIXTURE / "schema.sql").read_text())
            applied = cargo(
                ["run", "--locked", "--quiet", "--bin", "migrate", "--", "up"], output, target_url
            )
            assert applied.returncode == 0, applied.stdout + applied.stderr
            source = source_run()
            candidate = target_run(tamper=False)
            assert [item["status"] for item in source] == [case["status"] for case in SCENARIOS]
            assert [item["status"] for item in candidate] == [case["status"] for case in SCENARIOS]
            assert canonical(candidate) == canonical(source)
            created = source[2]
            assert created["body"] == {
                "id": 1,
                "name": "alpha",
                "count": 7,
                "enabled": True,
                "note": None,
            }
            assert source[3]["body"] == {"error": "conflict"}
            assert source[16]["body"] is None and source[16]["media_type"] == ""
            assert source[-1]["rows"] == source[-1]["body"]
            assert [row["name"] for row in source[-1]["rows"]] == ["next", "float"]
            # The same responses cannot hide a different database state.
            reverted = cargo(
                ["run", "--locked", "--quiet", "--bin", "migrate", "--", "down"], output, target_url
            )
            assert reverted.returncode == 0, reverted.stdout + reverted.stderr
            assert (
                cargo(
                    ["run", "--locked", "--quiet", "--bin", "migrate", "--", "up"],
                    output,
                    target_url,
                ).returncode
                == 0
            )
            tampered = target_run(tamper=True)
            assert [item["status"] for item in tampered] == [case["status"] for case in SCENARIOS]
            assert canonical(tampered) != canonical(source)
            fmt = subprocess.run(
                ["cargo", "fmt", "--check"], cwd=output, capture_output=True, text=True, check=False
            )
            assert fmt.returncode == 0, fmt.stdout + fmt.stderr
        finally:
            for schema in schemas:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )

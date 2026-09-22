# SPDX-License-Identifier: Apache-2.0
"""Bounded replay of the captured contract with cargo and the real Express source.

Scenarios, observations and their comparison follow the shared
``sanka-http-replay`` contract, so the Go extension can run the same documents
against its own probes. Runners here are a generated ``cargo test`` probe for the
candidate and a Node script serving the transpiled source on a Unix socket.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from sanka_http_replay import (
    ScenarioError,
    cases_document,
    compare,
    default_scenarios,
    load_scenarios,
    validate_observations,
)
from sanka_ts_capture import node_executable, node_version, transpile_sources

from .capture import SCENARIO_FILE, canonical, capture, digest
from .render import RUST_VERSION, read_sql, render, rust_string

NODE_MAJOR = 22
PINNED_FILES = ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "contract.json")
DATABASE_PINNED = ("migrations/0001_initial.up.sql", "migrations/0001_initial.down.sql")
TARGET_DATABASE = "SANKA_RUST_TARGET_TEST_DATABASE_URL"
SOURCE_DATABASE = "SANKA_RUST_SOURCE_TEST_DATABASE_URL"
RESERVED = ("tests/sanka_contract_probe.rs", "sanka-cases.json", "sanka-observed.json")
EXPRESS_RUNNER = Path(__file__).resolve().parent / "node" / "express-run.js"
CARGO_TIMEOUT = 1200
REPLAY_SCHEMA = "sanka.typescript-to-rust.replay/v2"
FIELD_TYPES = {"i32": "integer", "i64": "bigint", "bool": "boolean", "String": "string"}

PROBE = """// SPDX-License-Identifier: Apache-2.0
use axum::body::{Body, to_bytes};
use axum::http::Request;
use serde_json::{Map, Value, json};
use tower::ServiceExt;
@IMPORTS@
#[tokio::test]
async fn sanka_contract_replay() {
    let raw = std::fs::read_to_string("sanka-cases.json").expect("cases");
    let document: Value = serde_json::from_str(&raw).expect("cases json");
    let scenarios = document["scenarios"].as_array().expect("scenarios").clone();
@SETUP@    let mut observed = Vec::new();
    for scenario in scenarios {
        let method = scenario["method"].as_str().expect("method").to_string();
        let path = scenario["path"].as_str().expect("path").to_string();
        let mut builder = Request::builder().method(method.as_str()).uri(&path);
        if let Some(headers) = scenario["headers"].as_object() {
            for (name, value) in headers {
                builder = builder.header(name.as_str(), value.as_str().expect("header"));
            }
        }
        let request = match scenario.get("body") {
            Some(value) => builder
                .header("content-type", "application/json")
                .body(Body::from(serde_json::to_vec(value).expect("encode body"))),
            None => builder.body(Body::empty()),
        }
        .expect("request");
        let response = @APP@
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
        let mut record = Map::new();
        record.insert("id".into(), scenario["id"].clone());
        record.insert("method".into(), Value::String(method));
        record.insert("path".into(), Value::String(path));
        record.insert("status".into(), json!(status));
        record.insert("media_type".into(), Value::String(media_type));
        record.insert("body".into(), body);
@TABLES@        observed.push(Value::Object(record));
    }
    let encoded = serde_json::to_vec(&json!({
        "schema": "sanka.http-observations/v1",
        "observations": observed,
    }))
    .expect("encode");
    std::fs::write("sanka-observed.json", encoded).expect("write");
}
"""

PROBE_SETUP = """    let url = std::env::var("DATABASE_URL").expect("DATABASE_URL must be set");
    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(1)
        .connect(&url)
        .await
        .expect("connect to DATABASE_URL");
"""

PROBE_TABLES_HEAD = """        let mut tables = Map::new();
        let mut sequences = Map::new();
"""

PROBE_TABLE = """        let rows = sqlx::query_as::<_, migrated_backend::models::@MODEL@>(@SQL@)
            .fetch_all(&pool)
            .await
            .ok()
            .map(|items| serde_json::to_value(items).expect("rows json"))
            .unwrap_or(Value::Null);
        tables.insert(@TABLE@.into(), rows);
"""

PROBE_SEQUENCE = """        let named: Option<Option<String>> = sqlx::query_scalar(SEQUENCE_NAME)
            .bind(@TABLE@)
            .bind(@KEY@)
            .fetch_one(&pool)
            .await
            .ok();
        let sequence = match named.flatten() {
            Some(name) => sqlx::query(&format!("{SEQUENCE_STATE} {name}"))
                .fetch_one(&pool)
                .await
                .ok()
                .map(|row| {
                    let value: String = row.get("value");
                    let called: bool = row.get("is_called");
                    json!([value, called])
                })
                .unwrap_or(Value::Null),
            None => Value::Null,
        };
        sequences.insert(@TABLE@.into(), sequence);
"""

PROBE_TABLES_TAIL = """        record.insert("tables".into(), Value::Object(tables));
        record.insert("sequences".into(), Value::Object(sequences));
"""

PROBE_IMPORTS = """use sqlx::Row;

const SEQUENCE_NAME: &str = "SELECT pg_get_serial_sequence($1, $2)";
const SEQUENCE_STATE: &str = "SELECT last_value::text AS value, is_called FROM";
"""


def _database_url(name: str) -> str:
    value = os.environ.get(name, "")
    if not value.startswith(("postgres://", "postgresql://")):
        raise ValueError(
            f"replay with database_layer sqlx requires {name} to name a dedicated "
            "PostgreSQL test database (postgres://...)"
        )
    return value


def _cargo(
    command: list[str],
    cwd: Path,
    target_dir: Path | None = None,
    database_url: str | None = None,
) -> str:
    environment = os.environ | {
        "CARGO_TERM_COLOR": "never",
        "CARGO_NET_RETRY": "2",
        "CARGO_INCREMENTAL": "0",
    }
    environment.pop("DATABASE_URL", None)
    if database_url is not None:
        environment["DATABASE_URL"] = database_url
    if target_dir is not None:
        # The candidate is compiled from a temporary copy; keep build artifacts beside the
        # output so repeated test/verify runs do not rebuild every dependency. An explicit
        # CARGO_TARGET_DIR in the environment (CI caches, local development) wins.
        environment.setdefault("CARGO_TARGET_DIR", str(target_dir))
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=CARGO_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"replay process failed: {error}") from error
    if result.returncode:
        raise ValueError("replay process failed: " + (result.stdout + result.stderr)[-4000:])
    return result.stdout


def _node(command: list[str], cwd: Path, modules: Path, database_url: str | None = None) -> None:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "NODE_PATH": str(modules),
        "NODE_OPTIONS": "",
    }
    if database_url is not None:
        environment["DATABASE_URL"] = database_url
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"source replay failed: {error}") from error
    if result.returncode:
        raise ValueError("source replay failed: " + (result.stdout + result.stderr)[-4000:])


def _toolchain(candidate: Path) -> str:
    version = _cargo(["rustc", "--version"], candidate).split()
    if len(version) < 2 or version[1] != RUST_VERSION:
        raise ValueError(f"replay requires the qualified Rust {RUST_VERSION} toolchain")
    cargo = _cargo(["cargo", "--version"], candidate).split()
    if len(cargo) < 2 or cargo[1] != RUST_VERSION:
        raise ValueError(f"replay requires cargo from the Rust {RUST_VERSION} toolchain")
    return " ".join(version[:2])


def _node_modules(root: Path, packages: tuple[str, ...]) -> Path:
    explicit = os.environ.get("SANKA_NODE_TOOLS")
    modules = Path(explicit) if explicit else root / "node_modules"
    for package in packages:
        if not (modules / package / "package.json").is_file():
            raise ValueError(
                f"verify requires the source project's {package} installation under "
                "node_modules, or SANKA_NODE_TOOLS pointing at a directory that contains it"
            )
    return modules.resolve()


def _snapshot(output: Path) -> dict[str, bytes]:
    if output.is_symlink() or not output.is_dir():
        raise ValueError("candidate must be a regular directory")
    if (output / "target").exists():
        raise ValueError("remove the target build directory from the candidate before replay")
    snapshot: dict[str, bytes] = {}
    size = 0
    for directory, names, filenames in os.walk(output, followlinks=False):
        if any((Path(directory) / name).is_symlink() for name in names):
            raise ValueError("candidate symlinks are unsupported")
        for name in filenames:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("candidate must contain only regular files")
            size += path.stat().st_size
            if size > 10_000_000 or len(snapshot) >= 1000:
                raise ValueError("candidate exceeds replay limits")
            snapshot[path.relative_to(output).as_posix()] = path.read_bytes()
    return snapshot


def operations(captured: dict[str, Any]) -> list[dict[str, Any]]:
    """The captured routes in the shared scenario generator's vocabulary."""
    result: list[dict[str, Any]] = []
    for route in captured["routes"]:
        record: dict[str, Any] = {"method": route["method"], "path": route["path"]}
        if "read" in route:
            record.update(kind="list", model=route["read"]["model"], status=route["status"])
        elif "lookup" in route:
            record.update(kind="lookup", model=route["lookup"]["model"])
        elif "write" in route:
            write = route["write"]
            record.update(kind=write["operation"], model=write["model"])
            if "conflict" in write:
                record["conflict"] = bool(write["conflict"])
        else:
            record.update(kind="literal", status=route["status"])
        result.append(record)
    return result


def scenario_models(captured: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": model["name"],
            "table": model["table"],
            "fields": [
                {
                    "name": field["name"],
                    "type": FIELD_TYPES[field["rust_type"]],
                    "nullable": bool(field["nullable"]),
                    "primary_key": bool(field["primary_key"]),
                    "auto": bool(field["auto"]),
                    "unique": bool(field["unique"]),
                }
                for field in model["fields"]
            ],
        }
        for model in captured.get("models", [])
    ]


def scenarios_for(root: Path, captured: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Declared ``sanka-verify.json`` scenarios when present, otherwise the defaults."""
    declared = root / SCENARIO_FILE
    if declared.exists():
        return load_scenarios(declared), "declared"
    return default_scenarios(operations(captured), scenario_models(captured)), "default"


def _probe(captured: dict[str, Any], database: bool) -> str:
    tables = ""
    if database:
        parts = [PROBE_TABLES_HEAD]
        for model in captured.get("models", []):
            parts.append(
                PROBE_TABLE.replace("@MODEL@", model["name"])
                .replace("@SQL@", rust_string(read_sql(model).rsplit(" LIMIT", 1)[0]))
                .replace("@TABLE@", rust_string(model["table"]))
            )
            primary = next(field for field in model["fields"] if field["primary_key"])
            if primary["auto"]:
                parts.append(
                    PROBE_SEQUENCE.replace("@TABLE@", rust_string(model["table"])).replace(
                        "@KEY@", rust_string(primary["name"])
                    )
                )
        parts.append(PROBE_TABLES_TAIL)
        tables = "".join(parts)
    return (
        PROBE.replace("@IMPORTS@", PROBE_IMPORTS if database else "")
        .replace("@SETUP@", PROBE_SETUP if database else "")
        .replace(
            "@APP@",
            "migrated_backend::app(pool.clone())" if database else "migrated_backend::app()",
        )
        .replace("@TABLES@", tables)
    )


def replay(root: Path, output: Path, captured: dict[str, Any], command: str) -> dict[str, Any]:
    if captured["gaps"]:
        raise ValueError("cannot replay unsupported source behavior")
    if not output.is_dir():
        raise ValueError("apply the reviewed plan before testing")
    snapshot = _snapshot(output)
    # Bind evidence to the exact bytes tested, including manually repaired handlers.
    candidate_hash = digest(
        {key: hashlib.sha256(value).hexdigest() for key, value in snapshot.items()}
    )
    config = captured["configuration"]
    database = config.get("database_layer") == "sqlx"
    writes = any("write" in route for route in captured["routes"])
    target_url = _database_url(TARGET_DATABASE) if database else None
    source_url = _database_url(SOURCE_DATABASE) if database and command == "verify" else None
    if database and command == "verify" and source_url == target_url:
        raise ValueError("source and target test databases must be distinct")
    expected_files = render(captured)
    for name in PINNED_FILES + (DATABASE_PINNED if database else ()):
        if snapshot.get(name) != expected_files[name].encode():
            raise ValueError(f"candidate {name} differs from the applied plan")
    source_text = (root / config["source_file"]).read_text(encoding="utf-8")
    schema_text = (root / config["schema_file"]).read_text(encoding="utf-8") if database else ""
    try:
        scenarios, origin = scenarios_for(root, captured)
    except ScenarioError as error:
        raise ValueError(f"scenarios: {error}") from error
    if capture(root, config) != captured:
        raise ValueError("source changed before replay")
    with tempfile.TemporaryDirectory(prefix="sanka-rust-replay-") as temporary:
        workspace = Path(temporary)
        candidate = workspace / "candidate"
        candidate.mkdir()
        for name, content in snapshot.items():
            destination = candidate / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        if any((candidate / name).exists() for name in RESERVED):
            raise ValueError("candidate uses reserved replay filenames")
        (candidate / "tests").mkdir(exist_ok=True)
        (candidate / "tests" / "sanka_contract_probe.rs").write_text(_probe(captured, database))
        cases = canonical(cases_document(scenarios))
        (candidate / "sanka-cases.json").write_text(cases)
        rust_version = _toolchain(candidate)
        offline = ["--offline"] if os.environ.get("SANKA_RUST_OFFLINE") == "1" else []
        target_dir = output.parent / "rust-target"
        target_dir.mkdir(exist_ok=True)
        if writes:
            # Write contracts start from the captured baseline on the target database.
            for direction in ("down", "up"):
                _cargo(
                    [
                        "cargo",
                        "run",
                        "--locked",
                        "--quiet",
                        *offline,
                        "--bin",
                        "migrate",
                        "--",
                        direction,
                    ],
                    candidate,
                    target_dir.resolve(),
                    target_url,
                )
        _cargo(
            ["cargo", "test", "--locked", "--quiet", *offline, "--test", "sanka_contract_probe"],
            candidate,
            target_dir.resolve(),
            target_url,
        )
        actual = validate_observations(
            json.loads((candidate / "sanka-observed.json").read_text()), scenarios
        )
        comparison = compare(scenarios, actual)
        result: dict[str, Any] = {
            "schema": REPLAY_SCHEMA,
            "command": command,
            "rust_version": rust_version,
            "source_digest": captured["source_digest"],
            "candidate_digest": candidate_hash,
            "scenarios": scenarios,
            "scenario_origin": origin,
            "scope": (
                "status, media type, JSON body, captured table rows and identity sequences "
                "for every scenario, source against candidate"
                if command == "verify"
                else "axum handler execution, JSON responses and expected statuses per scenario"
            ),
            "complete_backend": False,
            "candidate": actual,
            "comparison": comparison,
            "ok": comparison["ok"],
        }
        if database:
            result["database_scope"] = (
                "captured tables reset to the baseline before write contracts; "
                "read-only contracts replay against the supplied fixtures as they are"
            )
        if command == "verify":
            node = node_executable()
            version = node_version(node)
            if version[0] != NODE_MAJOR:
                raise ValueError(
                    f"verify requires Node.js {NODE_MAJOR}.x to execute the source "
                    f"(found {version[0]}); set SANKA_NODE"
                )
            modules = _node_modules(root, ("express", "pg") if database else ("express",))
            transpiled = transpile_sources({config["source_file"]: source_text}, node=node)
            source_directory = workspace / "source"
            source_directory.mkdir()
            (source_directory / "app.js").write_text(transpiled[config["source_file"]])
            (source_directory / "cases.json").write_text(cases)
            observed = source_directory / "source-observed.json"
            arguments = [node, str(EXPRESS_RUNNER), "app.js", "cases.json", str(observed)]
            if database:
                spec = {
                    "reset": writes,
                    "schema": schema_text,
                    "models": [
                        {
                            "table": model["table"],
                            "columns": [field["name"] for field in model["fields"]],
                            "primary_key": next(
                                field["name"] for field in model["fields"] if field["primary_key"]
                            ),
                            "auto": next(
                                bool(field["auto"])
                                for field in model["fields"]
                                if field["primary_key"]
                            ),
                        }
                        for model in captured.get("models", [])
                    ],
                }
                (source_directory / "database.json").write_text(canonical(spec))
                arguments.append("database.json")
            _node(arguments, source_directory, modules, source_url)
            expected = validate_observations(json.loads(observed.read_text()), scenarios)
            comparison = compare(scenarios, actual, expected)
            result.update(
                source=expected,
                node_version="v" + ".".join(str(item) for item in version),
                comparison=comparison,
                ok=comparison["ok"],
            )
        if capture(root, config) != captured:
            raise ValueError("source changed during replay; discard observations")
        if _snapshot(output) != snapshot:
            raise ValueError("candidate changed during replay; discard observations")
        return result

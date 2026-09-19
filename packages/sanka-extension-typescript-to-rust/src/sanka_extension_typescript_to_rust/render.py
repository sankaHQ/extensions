# SPDX-License-Identifier: Apache-2.0
"""Pinned axum rendering for captured JSON GET contracts."""

from __future__ import annotations

import re
from importlib.resources import files
from typing import Any

from .capture import canonical
from .database import render_database
from .writes import delete_sql, insert_sql, replace_sql, select_sql, update_sql, writable

RUST_VERSION = "1.93.1"
CRATE = "migrated-backend"
LIBRARY = "migrated_backend"

CARGO_TOML = f"""[package]
name = "{CRATE}"
version = "0.1.0"
edition = "2024"
rust-version = "1.93"
publish = false

[lib]
name = "{LIBRARY}"
path = "src/lib.rs"

[[bin]]
name = "{CRATE}"
path = "src/main.rs"

[dependencies]
axum = {{ version = "0.8", default-features = false, features = ["http1", "tokio", "json"] }}
tokio = {{ version = "1", features = ["rt-multi-thread", "macros", "net", "signal"] }}
serde_json = "1"
@DATABASE@
[dev-dependencies]
tower = {{ version = "0.5", features = ["util"] }}
"""

DATABASE_DEPENDENCIES = (
    'serde = { version = "1", features = ["derive"] }\n'
    'sqlx = { version = "0.8", default-features = false, '
    'features = ["runtime-tokio", "postgres", "macros", "migrate"] }\n'
)

TOOLCHAIN = f"""[toolchain]
channel = "{RUST_VERSION}"
profile = "minimal"
"""

LIB_RS = """// SPDX-License-Identifier: Apache-2.0
// Generated experimental endpoint contract; not a complete backend migration.
use axum::Router;
use axum::http::{HeaderValue, StatusCode, header};
use axum::response::IntoResponse;
use axum::routing::get;

/// The captured route contract. Callers own serving, shutdown and deployment settings.
pub fn app() -> Router {
    let mut router = Router::new();
@ROUTES@
    router
}

@HANDLERS@"""

LIB_RS_DATABASE = """// SPDX-License-Identifier: Apache-2.0
// Generated experimental endpoint contract; not a complete backend migration.
pub mod models;

use axum::Router;
@BYTES@use axum::extract::@EXTRACT@;
use axum::http::{HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::@ROUTING@;
@SERDE@use sqlx::PgPool;

/// The captured route contract. Callers own the pool, serving, shutdown and deployment.
pub fn app(pool: PgPool) -> Router {
    let mut router = Router::new();
@ROUTES@
    router.with_state(pool)
}

fn json_static(status: StatusCode, body: &'static str) -> Response {
    let headers = [(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    )];
    (status, headers, body).into_response()
}

fn json_bytes(status: StatusCode, body: Vec<u8>) -> Response {
    let headers = [(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    )];
    (status, headers, body).into_response()
}

fn database_failure() -> Response {
    json_static(
        StatusCode::INTERNAL_SERVER_ERROR,
        "{\\"error\\":\\"database read failed\\"}",
    )
}
@HELPERS@
@HANDLERS@"""

PARAM_HELPERS = """
fn not_found() -> Response {
    json_static(StatusCode::NOT_FOUND, "{\\"error\\":\\"not found\\"}")
}

/// JavaScript `Number(text)` followed by `Number.isInteger` for decimal spellings.
fn route_id(raw: &str) -> Option<i64> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Some(0);
    }
    let number: f64 = trimmed.parse().ok()?;
    if !number.is_finite() || number.fract() != 0.0 || number.abs() > 9_007_199_254_740_992.0 {
        return None;
    }
    Some(number as i64)
}
"""

WRITE_HELPERS = """
fn write_failure() -> Response {
    json_static(
        StatusCode::INTERNAL_SERVER_ERROR,
        "{\\"error\\":\\"database write failed\\"}",
    )
}

fn write_error(error: sqlx::Error) -> Response {
    match error {
        sqlx::Error::Database(details) if details.code().as_deref() == Some("23505") => {
            json_static(StatusCode::CONFLICT, "{\\"error\\":\\"conflict\\"}")
        }
        _ => write_failure(),
    }
}
"""

BODY_HELPERS = """
fn invalid_body() -> Response {
    json_static(
        StatusCode::BAD_REQUEST,
        "{\\"error\\":\\"invalid request body\\"}",
    )
}

fn json_object(body: &Bytes) -> Option<Map<String, Value>> {
    match serde_json::from_slice::<Value>(body).ok()? {
        Value::Object(map) => Some(map),
        _ => None,
    }
}

fn only_fields(map: &Map<String, Value>, allowed: &[&str]) -> bool {
    map.keys().all(|key| allowed.contains(&key.as_str()))
}

/// A JSON number that JavaScript's `Number.isInteger` accepts within the i32 range.
fn integer_field(value: &Value) -> Option<i32> {
    let number = value.as_f64()?;
    if number.fract() != 0.0 || !(-2_147_483_648.0..=2_147_483_647.0).contains(&number) {
        return None;
    }
    Some(number as i32)
}
"""

LOOKUP_HANDLER = """@CONST@

async fn route_@INDEX@(State(pool): State<PgPool>, Path(raw): Path<String>) -> Response {
    let Some(id) = route_id(&raw) else {
        return not_found();
    };
    let found = sqlx::query_as::<_, models::@MODEL@>(ROUTE_@INDEX@_SQL)
        .bind(id)
        .fetch_optional(&pool)
        .await;
    match found {
        Ok(Some(row)) => match serde_json::to_vec(&row) {
            Ok(body) => json_bytes(StatusCode::OK, body),
            Err(_) => database_failure(),
        },
        Ok(None) => not_found(),
        Err(_) => database_failure(),
    }
}
"""

DELETE_HANDLER = """@CONST@

async fn route_@INDEX@(State(pool): State<PgPool>, Path(raw): Path<String>) -> Response {
    let Some(id) = route_id(&raw) else {
        return not_found();
    };
    let deleted = sqlx::query(ROUTE_@INDEX@_SQL)
        .bind(id)
        .fetch_optional(&pool)
        .await;
    match deleted {
        Ok(Some(_)) => StatusCode::NO_CONTENT.into_response(),
        Ok(None) => not_found(),
        Err(_) => write_failure(),
    }
}
"""

CREATE_HANDLER = """@CONST@
const ROUTE_@INDEX@_FIELDS: [&str; @COUNT@] = [@FIELDS@];

async fn route_@INDEX@(State(pool): State<PgPool>, body: Bytes) -> Response {
    let Some(map) = json_object(&body) else {
        return invalid_body();
    };
    if !only_fields(&map, &ROUTE_@INDEX@_FIELDS) {
        return invalid_body();
    }
@EXTRACT@
    let inserted = sqlx::query_as::<_, models::@MODEL@>(ROUTE_@INDEX@_SQL)
@BINDS@
        .fetch_one(&pool)
        .await;
    match inserted {
        Ok(row) => match serde_json::to_vec(&row) {
            Ok(body) => json_bytes(StatusCode::CREATED, body),
            Err(_) => write_failure(),
        },
        Err(error) => write_error(error),
    }
}
"""

UPDATE_HANDLER = """@CONST@
const ROUTE_@INDEX@_FIELDS: [&str; @COUNT@] = [@FIELDS@];

@SIGNATURE@
    let Some(id) = route_id(&raw) else {
        return not_found();
    };
    let Some(map) = json_object(&body) else {
        return invalid_body();
    };
    if !only_fields(&map, &ROUTE_@INDEX@_FIELDS) {
        return invalid_body();
    }
@EXTRACT@
    let updated = sqlx::query_as::<_, models::@MODEL@>(ROUTE_@INDEX@_SQL)
        .bind(id)
@BINDS@
        .fetch_optional(&pool)
        .await;
    match updated {
        Ok(Some(row)) => match serde_json::to_vec(&row) {
            Ok(body) => json_bytes(StatusCode::OK, body),
            Err(_) => write_failure(),
        },
        Ok(None) => not_found(),
        Err(error) => write_error(error),
    }
}
"""

READ_HANDLER = """@CONST@

async fn route_@INDEX@(State(pool): State<PgPool>) -> Response {
    let rows = sqlx::query_as::<_, models::@MODEL@>(ROUTE_@INDEX@_SQL)
        .bind(@LIMIT@_i64)
        .fetch_all(&pool)
        .await;
    let body = match rows.map(|items| serde_json::to_vec(&items)) {
        Ok(Ok(body)) => body,
        _ => return database_failure(),
    };
    let headers = [(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    )];
    (StatusCode::OK, headers, body).into_response()
}
"""

HANDLER = """async fn route_@INDEX@() -> impl IntoResponse {
    (
        @STATUS@,
        [(
            header::CONTENT_TYPE,
            HeaderValue::from_static("application/json"),
        )],
        @BODY@,
    )
}
"""

MAIN_RS = """// SPDX-License-Identifier: Apache-2.0
// Generated serving entrypoint. Bind address and port are explicit environment inputs.
use std::net::{IpAddr, SocketAddr};

#[tokio::main]
async fn main() {
    let host: IpAddr = std::env::var("HOST")
        .unwrap_or_else(|_| "127.0.0.1".to_string())
        .parse()
        .expect("HOST must be an IP address");
    let port: u16 = std::env::var("PORT")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(3000);
    let listener = tokio::net::TcpListener::bind(SocketAddr::new(host, port))
        .await
        .expect("bind the configured address");
    axum::serve(listener, migrated_backend::app())
        .with_graceful_shutdown(shutdown())
        .await
        .expect("serve");
}

async fn shutdown() {
    let _ = tokio::signal::ctrl_c().await;
}
"""

MAIN_RS_DATABASE = """// SPDX-License-Identifier: Apache-2.0
// Generated serving entrypoint. Bind address, port and database are explicit inputs.
use std::net::{IpAddr, SocketAddr};

use sqlx::postgres::PgPoolOptions;

#[tokio::main]
async fn main() {
    let host: IpAddr = std::env::var("HOST")
        .unwrap_or_else(|_| "127.0.0.1".to_string())
        .parse()
        .expect("HOST must be an IP address");
    let port: u16 = std::env::var("PORT")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(3000);
    let url = std::env::var("DATABASE_URL").expect("DATABASE_URL must be set");
    let pool = PgPoolOptions::new()
        .max_connections(5)
        .connect(&url)
        .await
        .expect("connect to DATABASE_URL");
    let listener = tokio::net::TcpListener::bind(SocketAddr::new(host, port))
        .await
        .expect("bind the configured address");
    axum::serve(listener, migrated_backend::app(pool))
        .with_graceful_shutdown(shutdown())
        .await
        .expect("serve");
}

async fn shutdown() {
    let _ = tokio::signal::ctrl_c().await;
}
"""

README = """# Migrated backend (experimental endpoint contract)

Generated by `sanka/typescript-to-rust` from a captured Express application.
It reproduces @COUNT@ literal JSON GET route(s); see `contract.json` for the exact
captured paths, statuses and bodies. This is not a complete backend migration.

```sh
cargo build --locked
HOST=127.0.0.1 PORT=3000 cargo run --locked
cargo test --locked
```

`rust-toolchain.toml` pins the qualified toolchain. Express defaults that are not
reproduced: case-insensitive and non-strict (trailing slash) routing, the
`X-Powered-By` and `ETag` headers, and `charset=utf-8` on `Content-Type`.
@DATABASE@"""

README_DATABASE = """
## Database

`schema.sql` and the captured row types became `src/models.rs` and the reversible
`sqlx` baseline under `migrations/`. The baseline targets an empty schema and
refuses to run where a captured table already exists. `DATABASE_URL` is required
by the server, `cargo test` and the migration runner; see `.env.example`.

```sh
DATABASE_URL=postgres://... cargo run --locked --bin migrate -- up
DATABASE_URL=postgres://... cargo run --locked --bin migrate -- down
```

Captured reads select every column of one table ordered by its primary key with a
literal limit. `bigint` columns are serialized as decimal strings, matching the
node-postgres default the source application exposed.
"""


def rust_string(value: str) -> str:
    escaped = []
    for character in value:
        if character == "\\":
            escaped.append("\\\\")
        elif character == '"':
            escaped.append('\\"')
        elif ord(character) < 0x20 or character == "\x7f":
            escaped.append(f"\\u{{{ord(character):x}}}")
        else:
            escaped.append(character)
    return '"' + "".join(escaped) + '"'


def render(captured: dict[str, Any]) -> dict[str, str]:
    if captured["gaps"]:
        raise ValueError("resolve source capture gaps before generation")
    target = captured["configuration"]["target_framework"]
    if target != "axum":
        raise ValueError(f"unsupported target framework: {target}")
    database = captured["configuration"].get("database_layer") == "sqlx"
    models = {model["name"]: model for model in captured.get("models", [])} if database else {}
    registrations: list[str] = []
    handlers: list[str] = []
    grouped: dict[str, list[tuple[str, int]]] = {}
    for index, route in enumerate(captured["routes"]):
        grouped.setdefault(str(route["path"]), []).append((str(route["method"]), index))
    for route_path, members in grouped.items():
        chain = ".".join(
            f"{method.lower()}(route_{index})"
            for method, index in sorted(members, key=lambda item: METHOD_ORDER.index(item[0]))
        )
        line = f"    router = router.route({rust_string(axum_path(route_path))}, {chain});"
        if len(line) > 100:
            # rustfmt splits an overlong call into one argument per line.
            line = (
                f"    router = router.route(\n        {rust_string(axum_path(route_path))},"
                f"\n        {chain},\n    );"
            )
        registrations.append(line)
    for index, route in enumerate(captured["routes"]):
        if "read" in route:
            handlers.append(_read_handler(index, route["read"], models))
            continue
        if "lookup" in route:
            handlers.append(_lookup_handler(index, route["lookup"], models))
            continue
        if "write" in route:
            handlers.append(_write_handler(index, route["write"], models))
            continue
        status = (
            "StatusCode::OK"
            if route["status"] == 200
            else f'StatusCode::from_u16({route["status"]}).expect("captured status code")'
        )
        handlers.append(
            HANDLER.replace("@INDEX@", str(index))
            .replace("@STATUS@", status)
            .replace("@BODY@", rust_string(route["body_json"]))
        )
    lock = files("sanka_extension_typescript_to_rust").joinpath(
        "locks", f"{target}-postgresql" if database else target
    )
    library = LIB_RS_DATABASE if database else LIB_RS
    if database:
        routes = captured["routes"]
        methods = sorted(
            {
                sorted(members, key=lambda item: METHOD_ORDER.index(item[0]))[0][0].lower()
                for members in grouped.values()
            }
        )
        params = any("lookup" in route or route.get("write", {}).get("param") for route in routes)
        writes = any("write" in route for route in routes)
        bodies = any(
            route.get("write", {}).get("operation") in {"create", "update", "replace"}
            for route in routes
        )
        helpers = (
            (PARAM_HELPERS if params else "")
            + (WRITE_HELPERS if writes else "")
            + (BODY_HELPERS if bodies else "")
        )
        library = (
            library.replace("@BYTES@", "use axum::body::Bytes;\n" if bodies else "")
            .replace("@EXTRACT@", "{Path, State}" if params else "State")
            .replace(
                "@ROUTING@", methods[0] if len(methods) == 1 else "{" + ", ".join(methods) + "}"
            )
            .replace("@SERDE@", "use serde_json::{Map, Value};\n" if bodies else "")
            .replace("@HELPERS@", helpers)
        )
    rendered = {
        "Cargo.toml": CARGO_TOML.replace(
            "@DATABASE@\n", DATABASE_DEPENDENCIES + "\n" if database else "\n"
        ),
        "Cargo.lock": lock.joinpath("Cargo.lock").read_text(),
        "rust-toolchain.toml": TOOLCHAIN,
        "src/lib.rs": library.replace("@ROUTES@", "\n".join(registrations)).replace(
            "@HANDLERS@", "\n".join(handlers)
        ),
        "src/main.rs": MAIN_RS_DATABASE if database else MAIN_RS,
        "contract.json": canonical(captured) + "\n",
        "README.md": README.replace("@COUNT@", str(len(captured["routes"]))).replace(
            "@DATABASE@", README_DATABASE if database else ""
        ),
    }
    if database:
        rendered.update(render_database(list(models.values())))
    return rendered


METHOD_ORDER = ("GET", "POST", "PUT", "PATCH", "DELETE")


def axum_path(route_path: str) -> str:
    """Express ``/items/:id`` becomes axum 0.8's ``/items/{id}``."""
    head, separator, tail = route_path.rpartition("/:")
    return f"{head}/{{{tail}}}" if separator else route_path


def quote_sql(sql: str, model: dict[str, Any]) -> str:
    """Quote every captured identifier of the model in an unquoted canonical statement."""
    names = sorted(
        {model["table"], *(field["name"] for field in model["fields"])}, key=len, reverse=True
    )
    pattern = re.compile(r"\b(" + "|".join(re.escape(name) for name in names) + r")\b")
    return pattern.sub(lambda match: f'"{match.group(1)}"', sql)


def read_sql(model: dict[str, Any]) -> str:
    columns = ", ".join(f'"{field["name"]}"' for field in model["fields"])
    primary = next(field["name"] for field in model["fields"] if field["primary_key"])
    return f'SELECT {columns} FROM "{model["table"]}" ORDER BY "{primary}" LIMIT $1'


def sql_const(index: int, sql: str) -> str:
    literal = f'r#"{sql}"#'
    declaration = f"const ROUTE_{index}_SQL: &str = {literal};"
    if len(declaration) > 100 and len(literal) + 5 <= 100:
        # rustfmt breaks after `=` only when the literal then fits within max_width.
        declaration = f"const ROUTE_{index}_SQL: &str =\n    {literal};"
    return declaration


def _model(models: dict[str, dict[str, Any]], record: dict[str, Any]) -> dict[str, Any]:
    model = models.get(record["model"])
    if model is None or model["table"] != record["table"]:
        raise ValueError("captured route references an unknown model")
    return model


def _lookup_handler(index: int, lookup: dict[str, Any], models: dict[str, dict[str, Any]]) -> str:
    model = _model(models, lookup)
    return (
        LOOKUP_HANDLER.replace("@CONST@", sql_const(index, quote_sql(select_sql(model), model)))
        .replace("@INDEX@", str(index))
        .replace("@MODEL@", model["name"])
    )


def _extract(field: dict[str, Any], partial: bool) -> str:
    """Rust statements binding one request field with the canonical validation."""
    name, kind, nullable = field["name"], field["rust_type"], field["nullable"]
    key = rust_string(name)
    invalid = "return invalid_body()"
    if kind == "String":
        pattern, value = "Some(Value::String(text))", "text.clone()"
    elif kind == "bool":
        pattern, value = "Some(Value::Bool(flag))", "*flag"
    else:
        pattern = "Some(value)"
        value = (
            "match integer_field(value) {\n"
            "            Some(number) => number,\n"
            f"            None => {invalid},\n"
            "        }"
        )
    if partial and nullable:
        head = f"let ({name}_set, {name}) = match map.get({key}) {{"
        arms = [
            "None => (false, None),",
            "Some(Value::Null) => (true, None),",
            f"{pattern} => (true, Some({value})),",
        ]
    elif partial:
        head = f"let {name} = match map.get({key}) {{"
        arms = ["None => None,", f"{pattern} => Some({value}),"]
    elif nullable:
        head = f"let {name} = match map.get({key}) {{"
        arms = ["None | Some(Value::Null) => None,", f"{pattern} => Some({value}),"]
    else:
        head = f"let {name} = match map.get({key}) {{"
        arms = [f"{pattern} => {value},"]
    if kind != "i32":
        arms.append(f"_ => {invalid},")
    elif not (partial or nullable):
        arms.append(f"None => {invalid},")
    lines = ["    " + head] + ["        " + arm for arm in arms] + ["    };"]
    return "\n".join(lines)


def _write_handler(index: int, write: dict[str, Any], models: dict[str, dict[str, Any]]) -> str:
    model = _model(models, write)
    operation = write["operation"]
    if operation == "delete":
        return DELETE_HANDLER.replace(
            "@CONST@", sql_const(index, quote_sql(delete_sql(model), model))
        ).replace("@INDEX@", str(index))
    partial = operation == "update"
    fields = writable(model)
    sql = {"create": insert_sql, "update": update_sql, "replace": replace_sql}[operation](model)
    binds: list[str] = []
    for field in fields:
        if partial:
            flag = f"{field['name']}_set" if field["nullable"] else f"{field['name']}.is_some()"
            binds.append(f"        .bind({flag})")
        binds.append(f"        .bind({field['name']})")
    template = CREATE_HANDLER if operation == "create" else UPDATE_HANDLER
    signature = (
        f"async fn route_{index}(State(pool): State<PgPool>, Path(raw): Path<String>, "
        "body: Bytes) -> Response {"
    )
    if len(signature) > 100:
        # rustfmt puts one parameter per line once the signature exceeds max_width.
        signature = (
            f"async fn route_{index}(\n    State(pool): State<PgPool>,\n"
            "    Path(raw): Path<String>,\n    body: Bytes,\n) -> Response {"
        )
    return (
        template.replace("@SIGNATURE@", signature)
        .replace("@CONST@", sql_const(index, quote_sql(sql, model)))
        .replace("@INDEX@", str(index))
        .replace("@MODEL@", model["name"])
        .replace("@COUNT@", str(len(fields)))
        .replace("@FIELDS@", ", ".join(rust_string(field["name"]) for field in fields))
        .replace("@EXTRACT@", "\n".join(_extract(field, partial) for field in fields))
        .replace("@BINDS@", "\n".join(binds))
    )


def _read_handler(index: int, read: dict[str, Any], models: dict[str, dict[str, Any]]) -> str:
    model = models.get(read["model"])
    if model is None or model["table"] != read["table"]:
        raise ValueError("captured read references an unknown model")
    return (
        READ_HANDLER.replace("@CONST@", sql_const(index, read_sql(model)))
        .replace("@INDEX@", str(index))
        .replace("@MODEL@", model["name"])
        .replace("@LIMIT@", str(int(read["limit"])))
    )

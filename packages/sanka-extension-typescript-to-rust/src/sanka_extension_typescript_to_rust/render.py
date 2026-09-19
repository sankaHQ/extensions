# SPDX-License-Identifier: Apache-2.0
"""Pinned axum rendering for captured JSON GET contracts."""

from __future__ import annotations

from importlib.resources import files
from typing import Any

from .capture import canonical
from .database import render_database

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
use axum::extract::State;
use axum::http::{HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use sqlx::PgPool;

/// The captured route contract. Callers own the pool, serving, shutdown and deployment.
pub fn app(pool: PgPool) -> Router {
    let mut router = Router::new();
@ROUTES@
    router.with_state(pool)
}

fn database_failure() -> Response {
    let headers = [(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    )];
    let body = "{\\"error\\":\\"database read failed\\"}";
    (StatusCode::INTERNAL_SERVER_ERROR, headers, body).into_response()
}

@HANDLERS@"""

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
    for index, route in enumerate(captured["routes"]):
        registrations.append(
            f"    router = router.route({rust_string(route['path'])}, get(route_{index}));"
        )
        if "read" in route:
            handlers.append(_read_handler(index, route["read"], models))
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


def read_sql(model: dict[str, Any]) -> str:
    columns = ", ".join(f'"{field["name"]}"' for field in model["fields"])
    primary = next(field["name"] for field in model["fields"] if field["primary_key"])
    return f'SELECT {columns} FROM "{model["table"]}" ORDER BY "{primary}" LIMIT $1'


def _read_handler(index: int, read: dict[str, Any], models: dict[str, dict[str, Any]]) -> str:
    model = models.get(read["model"])
    if model is None or model["table"] != read["table"]:
        raise ValueError("captured read references an unknown model")
    literal = f'r#"{read_sql(model)}"#'
    declaration = f"const ROUTE_{index}_SQL: &str = {literal};"
    if len(declaration) > 100 and len(literal) + 5 <= 100:
        # rustfmt breaks after `=` only when the literal then fits within max_width.
        declaration = f"const ROUTE_{index}_SQL: &str =\n    {literal};"
    return (
        READ_HANDLER.replace("@CONST@", declaration)
        .replace("@INDEX@", str(index))
        .replace("@MODEL@", model["name"])
        .replace("@LIMIT@", str(int(read["limit"])))
    )

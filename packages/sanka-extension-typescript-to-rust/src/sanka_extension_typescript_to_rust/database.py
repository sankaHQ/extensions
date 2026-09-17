# SPDX-License-Identifier: Apache-2.0
"""Rendering of the sqlx database layer: row structs, a reversible baseline and a runner."""

from __future__ import annotations

from typing import Any

MODELS_HEADER = """// SPDX-License-Identifier: Apache-2.0
// Generated row types: one struct per captured table, columns in declaration order.
use serde::Serialize;
use sqlx::FromRow;
"""

BIGINT_HELPERS = """
// node-postgres serializes bigint columns as decimal strings; the contract keeps that shape.
pub fn bigint_as_string<S: serde::Serializer>(
    value: &i64,
    serializer: S,
) -> Result<S::Ok, S::Error> {
    serializer.serialize_str(&value.to_string())
}

pub fn nullable_bigint_as_string<S: serde::Serializer>(
    value: &Option<i64>,
    serializer: S,
) -> Result<S::Ok, S::Error> {
    match value {
        Some(value) => serializer.serialize_str(&value.to_string()),
        None => serializer.serialize_none(),
    }
}
"""

MIGRATE_RS = """// SPDX-License-Identifier: Apache-2.0
// Generated migration runner: `migrate up` applies the baseline, `migrate down` reverts it.
use sqlx::migrate::Migrator;
use sqlx::postgres::PgPoolOptions;

static MIGRATOR: Migrator = sqlx::migrate!("./migrations");

#[tokio::main]
async fn main() {
    let command = std::env::args().nth(1).unwrap_or_default();
    if command != "up" && command != "down" {
        eprintln!("usage: migrate <up|down>");
        std::process::exit(2);
    }
    let url = std::env::var("DATABASE_URL").expect("DATABASE_URL must be set");
    let pool = PgPoolOptions::new()
        .max_connections(1)
        .connect(&url)
        .await
        .expect("connect to DATABASE_URL");
    let result = if command == "up" {
        MIGRATOR.run(&pool).await
    } else {
        MIGRATOR.undo(&pool, 0).await
    };
    pool.close().await;
    if let Err(error) = result {
        eprintln!("migration failed: {error}");
        std::process::exit(1);
    }
}
"""

ENV_EXAMPLE = """# Export these before running the server, the tests or the migrate binary.
DATABASE_URL=postgres://user:password@localhost:5432/app
HOST=127.0.0.1
PORT=3000
"""


def render_database(models: list[dict[str, Any]]) -> dict[str, str]:
    """Return the generated files of the database layer keyed by crate-relative path."""
    return {
        "src/models.rs": models_rs(models),
        "migrations/0001_initial.up.sql": up_sql(models),
        "migrations/0001_initial.down.sql": down_sql(models),
        "src/bin/migrate.rs": MIGRATE_RS,
        ".env.example": ENV_EXAMPLE,
    }


def models_rs(models: list[dict[str, Any]]) -> str:
    parts = [MODELS_HEADER]
    if any(field["rust_type"] == "i64" for model in models for field in model["fields"]):
        parts.append(BIGINT_HELPERS)
    for model in models:
        lines = [
            "",
            "#[derive(Debug, Clone, Serialize, FromRow)]",
            f"pub struct {model['name']} {{",
        ]
        for field in model["fields"]:
            rust = f"Option<{field['rust_type']}>" if field["nullable"] else field["rust_type"]
            if field["rust_type"] == "i64":
                helper = "nullable_bigint_as_string" if field["nullable"] else "bigint_as_string"
                lines.append(f'    #[serde(serialize_with = "{helper}")]')
            lines.append(f"    pub {field['name']}: {rust},")
        lines.append("}")
        parts.append("\n".join(lines) + "\n")
    return "".join(parts)


def column_sql(field: dict[str, Any]) -> str:
    if field["identity"] == "serial":
        sql_type = "serial" if field["rust_type"] == "i32" else "bigserial"
    else:
        sql_type = field["sql_type"]
    parts = [f'"{field["name"]}"', sql_type]
    if field["identity"] in {"by default", "always"}:
        parts.append(f"GENERATED {field['identity'].upper()} AS IDENTITY")
    if field["primary_key"]:
        parts.append("PRIMARY KEY")
    elif not field["nullable"]:
        parts.append("NOT NULL")
    if field["unique"]:
        parts.append("UNIQUE")
    return " ".join(parts)


def up_sql(models: list[dict[str, Any]]) -> str:
    names = ", ".join(f"'{model['table']}'" for model in models)
    lines = [
        "-- Generated baseline for an empty schema.",
        "-- It refuses to run when a captured table already exists in the current schema.",
        "DO $$",
        "BEGIN",
        "    IF EXISTS (",
        "        SELECT 1 FROM information_schema.tables",
        f"        WHERE table_schema = current_schema() AND table_name IN ({names})",
        "    ) THEN",
        "        RAISE EXCEPTION 'schema_mode empty: captured tables already exist in schema %',",
        "            current_schema();",
        "    END IF;",
        "END",
        "$$;",
    ]
    for model in models:
        columns = [column_sql(field) for field in model["fields"]]
        lines += ["", f'CREATE TABLE "{model["table"]}" (']
        lines += [f"    {column}," for column in columns[:-1]] + [f"    {columns[-1]}"]
        lines.append(");")
    return "\n".join(lines) + "\n"


def down_sql(models: list[dict[str, Any]]) -> str:
    lines = ["-- Generated revert of the baseline: drops the captured tables in reverse order."]
    lines += [f'DROP TABLE IF EXISTS "{model["table"]}";' for model in reversed(models)]
    return "\n".join(lines) + "\n"

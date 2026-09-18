# SPDX-License-Identifier: Apache-2.0
"""Generate an explicit PostgreSQL baseline and migration runner."""

from __future__ import annotations

import json
from typing import Any

from .models import go_name

MIGRATION_RUNNER = """// SPDX-License-Identifier: Apache-2.0
package backend
import (
    "context"
    "database/sql"
    "embed"
    "fmt"
    "io/fs"
    _ "github.com/jackc/pgx/v5/stdlib"
    "github.com/pressly/goose/v3"
    "github.com/pressly/goose/v3/lock"
)
//go:embed migrations/*.sql
var migrationFiles embed.FS

// Migrate applies or rolls back the baseline only when explicitly called.
func Migrate(ctx context.Context, dsn string, direction string) error {
    if dsn == "" { return fmt.Errorf("DATABASE_URL is required") }
    if direction != "up" && direction != "down" {
        return fmt.Errorf("direction must be up or down")
    }
    db, err := sql.Open("pgx", dsn)
    if err != nil { return err }
    defer db.Close()
    if err := db.PingContext(ctx); err != nil { return err }
    migrations, err := fs.Sub(migrationFiles, "migrations")
    if err != nil { return err }
    locker, err := lock.NewPostgresSessionLocker()
    if err != nil { return err }
    provider, err := goose.NewProvider(goose.DialectPostgres, db, migrations,
        goose.WithSessionLocker(locker))
    if err != nil { return err }
    if direction == "up" { _, err = provider.Up(ctx) } else { _, err = provider.Down(ctx) }
    return err
}
"""

COMMAND = """// SPDX-License-Identifier: Apache-2.0
package main
import (
    "context"
    "fmt"
    "os"
    "os/signal"
    "syscall"
    "time"
    backend "migrated.backend"
)
func main() {
    if len(os.Args) != 2 || (os.Args[1] != "up" && os.Args[1] != "down") {
        fmt.Fprintln(os.Stderr, "usage: migrate up|down ({down_help})")
        os.Exit(2)
    }
    ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
    defer cancel()
    ctx, timeout := context.WithTimeout(ctx, 2*time.Minute)
    defer timeout()
    if err := backend.Migrate(ctx, os.Getenv("DATABASE_URL"), os.Args[1]); err != nil {
        fmt.Fprintln(os.Stderr, err)
        os.Exit(1)
    }
}
"""


def _jsonb(value: Any) -> str:
    return "'" + json.dumps(value, separators=(",", ":")).replace("'", "''") + "'::jsonb"


def _adoption_sql(captured: dict[str, Any]) -> list[str]:
    models = captured["models"]
    framework = captured["configuration"]["source_framework"]
    tables = [[model["table"], "r"] for model in models]
    columns = []
    constraints = []
    auto_columns = []
    for model in models:
        primary = []
        for ordinal, field in enumerate(model["fields"], 1):
            sql_type = field["sql_type"]
            length = int(sql_type[8:-1]) if sql_type.startswith("varchar(") else None
            data_type = "character varying" if length is not None else sql_type
            auto_kind = (
                "identity"
                if field["auto"] and framework == "drf"
                else ("sequence" if field["auto"] else "none")
            )
            columns.append(
                [
                    model["table"],
                    field["name"],
                    ordinal,
                    data_type,
                    field["nullable"],
                    length,
                    auto_kind,
                    "NEVER",
                ]
            )
            if field["auto"]:
                auto_columns.append([model["table"], field["name"]])
            if field["primary_key"]:
                primary.append(field["name"])
            elif field["unique"]:
                constraints.append([model["table"], "u", [field["name"]], False, False])
        constraints.append([model["table"], "p", primary, False, False])
    constraints.sort(key=lambda item: (item[0], item[1], item[2]))
    locks = "\n".join(f'LOCK TABLE "{model["table"]}" IN ACCESS SHARE MODE;' for model in models)
    auto_checks = "\n".join(
        f"""    IF pg_get_serial_sequence(
        format('%I.%I', current_schema(), '{table}'), '{column}'
    ) IS NULL THEN
        RAISE EXCEPTION 'schema adoption failed: sequence ownership differs';
    END IF;"""
        for table, column in auto_columns
    )
    return [
        "-- +goose Up",
        "-- +goose StatementBegin",
        f"""DO $sanka$
DECLARE actual jsonb;
BEGIN
    SELECT COALESCE(jsonb_agg(jsonb_build_array(c.relname, c.relkind) ORDER BY c.relname), '[]')
      INTO actual
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = current_schema() AND c.relkind IN ('r','p','v','m','f')
       AND c.relname <> 'goose_db_version';
    IF actual <> {_jsonb(tables)} THEN
        RAISE EXCEPTION 'schema adoption failed: table set differs';
    END IF;
END
$sanka$;""",
        "-- +goose StatementEnd",
        locks,
        "-- +goose StatementBegin",
        f"""DO $sanka$
DECLARE actual jsonb;
        sequence_count integer;
BEGIN
    SELECT COALESCE(jsonb_agg(jsonb_build_array(c.relname, c.relkind) ORDER BY c.relname), '[]')
      INTO actual
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = current_schema() AND c.relkind IN ('r','p','v','m','f')
       AND c.relname <> 'goose_db_version';
    IF actual <> {_jsonb(tables)} THEN
        RAISE EXCEPTION 'schema adoption failed: table set differs';
    END IF;

    SELECT COALESCE(jsonb_agg(jsonb_build_array(
        table_name, column_name, ordinal_position, data_type, is_nullable = 'YES',
        character_maximum_length,
        CASE WHEN is_identity = 'YES' THEN 'identity'
             WHEN column_default LIKE 'nextval(%' THEN 'sequence'
             WHEN column_default IS NULL THEN 'none' ELSE 'default' END,
        is_generated
    ) ORDER BY table_name, ordinal_position), '[]')
      INTO actual
      FROM information_schema.columns
     WHERE table_schema = current_schema() AND table_name <> 'goose_db_version';
    IF actual <> {_jsonb(columns)} THEN
        RAISE EXCEPTION 'schema adoption failed: columns differ';
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_constraint con
        JOIN pg_class c ON c.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema() AND c.relname <> 'goose_db_version'
          AND con.contype NOT IN ('p','u')
    ) THEN
        RAISE EXCEPTION 'schema adoption failed: unsupported constraints';
    END IF;
    SELECT COALESCE(jsonb_agg(jsonb_build_array(
        table_name, constraint_type, columns, deferrable, deferred
    ) ORDER BY table_name, constraint_type, columns), '[]')
      INTO actual
      FROM (
        SELECT c.relname AS table_name, con.contype::text AS constraint_type,
               jsonb_agg(a.attname ORDER BY key.ordinality) AS columns,
               con.condeferrable AS deferrable, con.condeferred AS deferred
        FROM pg_constraint con
        JOIN pg_class c ON c.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN unnest(con.conkey) WITH ORDINALITY AS key(attnum, ordinality) ON true
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = key.attnum
        WHERE n.nspname = current_schema() AND c.relname <> 'goose_db_version'
          AND con.contype IN ('p','u')
        GROUP BY c.relname, con.oid, con.contype, con.condeferrable, con.condeferred
      ) constraints;
    IF actual <> {_jsonb(constraints)} THEN
        RAISE EXCEPTION 'schema adoption failed: constraints differ';
    END IF;

    SELECT count(*)::int INTO sequence_count
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = current_schema() AND c.relkind = 'S'
       AND c.relname <> 'goose_db_version_id_seq';
    IF sequence_count <> {len(auto_columns)} THEN
        RAISE EXCEPTION 'schema adoption failed: sequence count differs';
    END IF;
{auto_checks}
    IF EXISTS (
        SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema() AND c.relname <> 'goose_db_version'
          AND NOT t.tgisinternal
    ) THEN
        RAISE EXCEPTION 'schema adoption failed: user triggers are unsupported';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema() AND c.relname <> 'goose_db_version'
          AND (c.relrowsecurity OR c.relforcerowsecurity)
    ) THEN
        RAISE EXCEPTION 'schema adoption failed: row security is unsupported';
    END IF;
END
$sanka$;""",
        "-- +goose StatementEnd",
        "-- +goose Down",
        "SELECT 1;",
    ]


def render_database(captured: dict[str, Any]) -> dict[str, str]:
    models = captured["models"]
    definitions = []
    if captured["configuration"]["schema_mode"] == "adopt-existing":
        sql = _adoption_sql(captured)
        down_help = "down preserves adopted application tables"
    else:
        sql = [
            "-- +goose Up",
            "-- +goose StatementBegin",
            """DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname=current_schema() AND c.relkind IN ('r','p','v','m','S','f')
               AND c.relname NOT IN ('goose_db_version','goose_db_version_id_seq')) THEN
        RAISE EXCEPTION 'baseline requires an empty schema; existing tables are never adopted';
    END IF;
END; $$;""",
            "-- +goose StatementEnd",
        ]
        down_help = "down deletes migrated tables"
    for model in models:
        lines = []
        columns = []
        for field in model["fields"]:
            kind = field["sql_type"]
            suffix = ""
            if field["auto"]:
                if captured["configuration"]["source_framework"] == "drf":
                    suffix += " GENERATED BY DEFAULT AS IDENTITY"
                else:
                    kind = "bigserial" if kind == "bigint" else "serial"
            if not field["nullable"]:
                suffix += " NOT NULL"
            if field["primary_key"]:
                suffix += " PRIMARY KEY"
            elif field["unique"]:
                suffix += " UNIQUE"
            columns.append(f'    "{field["name"]}" {kind}{suffix}')
            go_type = ("*" if field["nullable"] else "") + field["go_type"]
            lines.append(
                f"    {go_name(field['name'])} {go_type} "
                f'`json:"{field["name"]}" db:"{field["name"]}"`'
            )
        definitions.append(f"type {model['name']} struct {{\n" + "\n".join(lines) + "\n}")
        if captured["configuration"]["schema_mode"] == "empty":
            sql.append(f'CREATE TABLE "{model["table"]}" (\n' + ",\n".join(columns) + "\n);")
    if captured["configuration"]["schema_mode"] == "empty":
        sql.append("-- +goose Down")
        for model in reversed(models):
            sql.append(f'DROP TABLE "{model["table"]}";')
    return {
        "models.go": "// SPDX-License-Identifier: Apache-2.0\npackage backend\n\n"
        + "\n\n".join(definitions)
        + "\n",
        "migrations/00001_initial.sql": "\n\n".join(sql) + "\n",
        "database.go": MIGRATION_RUNNER,
        "cmd/migrate/main.go": COMMAND.replace("{down_help}", down_help),
        ".env.example": "# Supply the PostgreSQL URL at execution time.\nDATABASE_URL=\n",
    }

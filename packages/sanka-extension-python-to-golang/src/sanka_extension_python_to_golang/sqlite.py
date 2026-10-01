# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""SQLite database/sql transport for the existing captured query/transaction contract."""

from __future__ import annotations

import re
from typing import Any

GO_SQLITE = r"""// SPDX-License-Identifier: Apache-2.0
package backend

import (
    "context"
    "database/sql"
    "errors"
    "fmt"
    "net/url"
    "path/filepath"
    "regexp"
    "strings"
    "github.com/jackc/pgx/v5/pgconn"
    "modernc.org/sqlite"
)

type SQLitePool struct { db *sql.DB }
type sqliteTx struct { tx *sql.Tx }
type sqliteRows struct { *sql.Rows }
type sqliteRow struct { row *sql.Row }
type sqliteResult int64
func (r sqliteResult) RowsAffected() int64 { return int64(r) }

// OpenSQLite never runs migrations. Use cmd/migrate explicitly on a reviewed file.
func OpenSQLite(ctx context.Context, dsn string) (*SQLitePool, error) {
    parsed,err:=url.Parse(dsn)
    if err!=nil || parsed.Scheme!="file" || parsed.Host!="" || !filepath.IsAbs(parsed.Path) || parsed.Fragment!="" {
        return nil,fmt.Errorf("SQLite DATABASE_URL must be an absolute file: URI")
    }
    query:=parsed.Query()
    if len(query)!=0 { return nil,fmt.Errorf("SQLite DATABASE_URL must not contain connection options") }
    query.Add("_pragma","foreign_keys(1)");query.Add("_pragma","busy_timeout(5000)")
    parsed.RawQuery=query.Encode()
    db,err:=sql.Open("sqlite",parsed.String());if err!=nil { return nil,err }
    // ponytail: one SQLite writer; qualify a larger pool if concurrent write throughput needs it.
    db.SetMaxOpenConns(1)
    if err=db.PingContext(ctx);err!=nil { db.Close();return nil,err }
    return &SQLitePool{db},nil
}
func (p *SQLitePool) Close() { _=p.db.Close() }
func (p *SQLitePool) Ping(ctx context.Context) error { return p.db.PingContext(ctx) }
func (p *SQLitePool) Begin(ctx context.Context) (*sqliteTx,error) {
    tx,err:=p.db.BeginTx(ctx,nil);if err!=nil { return nil,err };return &sqliteTx{tx},nil
}
func sqliteBeginFunc(ctx context.Context,pool *SQLitePool,run func(*sqliteTx) error) error {
    tx,err:=pool.Begin(ctx);if err!=nil { return err };defer tx.Rollback(ctx)
    if err=run(tx);err!=nil { return err };return tx.Commit(ctx)
}
func (t *sqliteTx) Commit(context.Context) error { return sqliteError(t.tx.Commit()) }
func (t *sqliteTx) Rollback(context.Context) error { return t.tx.Rollback() }

var sqlitePlaceholder=regexp.MustCompile(`\$([0-9]+)`)
var sqliteText=regexp.MustCompile(`("[A-Za-z_][A-Za-z_0-9]*"|[A-Za-z_][A-Za-z_0-9]*)::text`)
func sqliteQuery(query string) string {
    query=sqlitePlaceholder.ReplaceAllString(query,"?$1")
    query=sqliteText.ReplaceAllString(query,"CAST($1 AS TEXT)")
    query=strings.ReplaceAll(query,` COLLATE "C"`,` COLLATE BINARY`)
    return query
}
func sqliteError(err error) error {
    var constraint *sqlite.Error
    if errors.As(err,&constraint) {
        code:=map[int]string{1555:"23505",2067:"23505",787:"23503",1299:"23502",275:"23514"}[constraint.Code()]
        if code!="" { return &pgconn.PgError{Code:code,Message:err.Error()} }
    }
    return err
}
func sqliteExec(ctx context.Context,exec func(context.Context,string,...any)(sql.Result,error),query string,args ...any)(sqliteResult,error) {
    result,err:=exec(ctx,sqliteQuery(query),args...);if err!=nil { return 0,sqliteError(err) }
    count,err:=result.RowsAffected();return sqliteResult(count),err
}
func (p *SQLitePool) Exec(ctx context.Context,query string,args ...any)(sqliteResult,error) {
    return sqliteExec(ctx,p.db.ExecContext,query,args...)
}
func (t *sqliteTx) Exec(ctx context.Context,query string,args ...any)(sqliteResult,error) {
    return sqliteExec(ctx,t.tx.ExecContext,query,args...)
}
func (p *SQLitePool) Query(ctx context.Context,query string,args ...any)(*sqliteRows,error) {
    rows,err:=p.db.QueryContext(ctx,sqliteQuery(query),args...);if err!=nil { return nil,sqliteError(err) };return &sqliteRows{rows},nil
}
func (t *sqliteTx) Query(ctx context.Context,query string,args ...any)(*sqliteRows,error) {
    rows,err:=t.tx.QueryContext(ctx,sqliteQuery(query),args...);if err!=nil { return nil,sqliteError(err) };return &sqliteRows{rows},nil
}
func (p *SQLitePool) QueryRow(ctx context.Context,query string,args ...any)*sqliteRow {
    return &sqliteRow{p.db.QueryRowContext(ctx,sqliteQuery(query),args...)}
}
func (t *sqliteTx) QueryRow(ctx context.Context,query string,args ...any)*sqliteRow {
    return &sqliteRow{t.tx.QueryRowContext(ctx,sqliteQuery(query),args...)}
}
func (r *sqliteRow) Scan(dest ...any) error { return sqliteError(r.row.Scan(dest...)) }
func (r *sqliteRows) Values() ([]any,error) {
    columns,err:=r.Columns();if err!=nil { return nil,err }
    values:=make([]any,len(columns));dest:=make([]any,len(columns))
    for i:=range values { dest[i]=&values[i] };err=r.Scan(dest...);return values,err
}
"""


def adapt_go(source: str) -> str:
    """Keep router/query generation shared; replace only its database transport."""
    source = source.replace('"github.com/jackc/pgx/v5/pgxpool";', "").replace(
        '"github.com/jackc/pgx/v5/pgxpool"', ""
    )
    source = source.replace(
        "    poolConfig, err := pgxpool.ParseConfig(cfg.databaseURL)\n"
        '    if err != nil { return fmt.Errorf("invalid DATABASE_URL") }\n',
        "",
    ).replace(
        "pgxpool.NewWithConfig(startupCtx, poolConfig)",
        "backend.OpenSQLite(startupCtx, cfg.databaseURL)",
    )
    source = source.replace("*pgxpool.Pool", "*SQLitePool").replace("pgxpool.New", "OpenSQLite")
    source = source.replace("pgx.Tx", "*sqliteTx").replace("pgx.BeginFunc", "sqliteBeginFunc")
    source = source.replace("pgx.ErrNoRows", "sql.ErrNoRows")
    if not re.search(r"\bpgx\.", source):
        source = source.replace(
            '"github.com/jackc/pgx/v5"', '"database/sql"' if "sql.ErrNoRows" in source else ""
        )
    return source


def adapt_files(files: dict[str, str]) -> dict[str, str]:
    return {
        **{
            name: adapt_go(text) if name.endswith(".go") and name != "database.go" else text
            for name, text in files.items()
        },
        "sqlite.go": GO_SQLITE,
    }


SQLITE_SOURCE_SNAPSHOT = """def snapshot(connection):
    tables, sequences = {}, {}
    for model in models:
        fields = model['fields']
        primary = next(field for field in fields if field['primary_key'])
        columns = ','.join('"' + field['name'] + '"' for field in fields)
        rows = connection.execute('SELECT ' + columns + ' FROM "' + model['table'] + '" ORDER BY "' + primary['name'] + '"').fetchall()
        tables[model['table']] = [{field['name']: (str(value) if field['go_type'] == 'int64' else bool(value) if field['go_type'] == 'bool' else value) if value is not None else None for field, value in zip(fields, row, strict=True)} for row in rows]
        if primary['auto']:
            value = connection.execute('SELECT COALESCE(MAX("' + primary['name'] + '"),0) FROM "' + model['table'] + '"').fetchone()[0]
            sequences[model['table']] = [str(value), value != 0]
    return tables, sequences
"""


def source_probe(probe: str, *, writes: bool = False) -> str:
    """The existing framework replay, against its own SQLite fixture only."""
    start = probe.index('        databases = {"default":')
    end = probe.index("    settings.configure", start)
    probe = (
        probe[:start]
        + "        databases = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': unquote(url.path)}}\n"
        + probe[end:]
    )
    if writes:
        start = probe.index("def snapshot(connection):")
        end = probe.index("observed = []", start)
        probe = probe[:start] + SQLITE_SOURCE_SNAPSHOT + probe[end:]
        probe = probe.replace("import psycopg\nfrom psycopg import sql", "import sqlite3")
        probe = probe.replace(
            "with psycopg.connect(os.environ['DATABASE_URL'].replace('postgresql+psycopg://', 'postgresql://'), autocommit=True) as connection:",
            "with sqlite3.connect(unquote(urlsplit(os.environ['DATABASE_URL']).path), isolation_level=None) as connection:",
        )
    else:
        probe = probe.replace(
            "model_spec.loader.exec_module(model_module)",
            "model_spec.loader.exec_module(model_module)\n    if framework == 'drf':\n        from django.db import connection, models\n        with connection.schema_editor() as editor:\n            for value in vars(model_module).values():\n                if isinstance(value, type) and issubclass(value, models.Model) and value.__module__ == model_module.__name__:\n                    editor.create_model(value)\n    else:\n        from sqlalchemy import create_engine\n        engine = create_engine(os.environ['DATABASE_URL'])\n        model_module.Base.metadata.create_all(engine)\n        engine.dispose()",
        )
    return probe


def snapshot_query(model: dict[str, Any]) -> str:
    """Portable JSON snapshots of only the captured integer/bool/text fields."""
    columns = []
    for field in model["fields"]:
        name = '"' + field["name"] + '"'
        value = name
        if field["go_type"] == "int64":
            value = "CAST(" + name + " AS TEXT)"
        elif field["go_type"] == "bool":
            value = (
                "json(CASE WHEN "
                + name
                + " IS NULL THEN 'null' WHEN "
                + name
                + " <> 0 THEN 'true' ELSE 'false' END)"
            )
        columns.extend(["'" + field["name"] + "'", value])
    primary = next(field for field in model["fields"] if field["primary_key"])
    return f'SELECT json_object({",".join(columns)}) FROM "{model["table"]}" ORDER BY "{primary["name"]}"'

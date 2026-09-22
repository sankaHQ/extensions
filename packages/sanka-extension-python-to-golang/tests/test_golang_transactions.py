# SPDX-License-Identifier: Apache-2.0
"""Qualify the pinned pgx transaction primitive before generating write handlers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from test_golang_schema import SOURCE_DDL, generate, schema_dsn

GO_TEST = r"""package backend

import (
	"context"
	"encoding/json"
	"errors"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"os"
	"testing"
	"time"
)

type faultTx struct {
	pgx.Tx
	closed                 bool
	commits, rollbacks     int
	commitErr, rollbackErr error
}

func (tx *faultTx) Commit(context.Context) error {
	tx.commits++
	tx.closed = true
	return tx.commitErr
}
func (tx *faultTx) Rollback(context.Context) error {
	if tx.closed {
		return pgx.ErrTxClosed
	}
	tx.rollbacks++
	tx.closed = true
	return tx.rollbackErr
}

type beginner struct {
	tx    *faultTx
	err   error
	calls int
}

func (b *beginner) Begin(context.Context) (pgx.Tx, error) {
	b.calls++
	return b.tx, b.err
}
func TestTransactionFaults(t *testing.T) {
	failure := errors.New("operation failed")
	for _, name := range []string{"begin", "work", "rollback", "commit", "success", "panic"} {
		t.Run(name, func(t *testing.T) {
			tx := &faultTx{}
			b := &beginner{tx: tx}
			if name == "begin" {
				b.err = failure
			}
			if name == "commit" {
				tx.commitErr = failure
			}
			if name == "rollback" {
				tx.rollbackErr = errors.New("cleanup failed")
			}
			calls := 0
			var err error
			var recovered any
			func() {
				defer func() { recovered = recover() }()
				err = pgx.BeginFunc(context.Background(), b, func(pgx.Tx) error {
					calls++
					if name == "panic" {
						panic(failure)
					}
					if name == "work" || name == "rollback" {
						return failure
					}
					return nil
				})
			}()
			if b.calls != 1 || calls > 1 {
				t.Fatal("transaction was retried")
			}
			if name == "begin" {
				if calls != 0 || tx.commits != 0 || tx.rollbacks != 0 {
					t.Fatal("work after begin failure")
				}
			} else if calls != 1 {
				t.Fatal("missing callback")
			}
			if name == "panic" {
				if recovered != failure || tx.rollbacks != 1 || tx.commits != 0 {
					t.Fatal("panic cleanup")
				}
			} else if name == "success" {
				if err != nil || tx.commits != 1 {
					t.Fatal("commit failed")
				}
			} else if !errors.Is(err, failure) {
				t.Fatal("original failure lost")
			}
			if name == "work" || name == "rollback" {
				if tx.commits != 0 || tx.rollbacks != 1 {
					t.Fatal("failed work was committed")
				}
			}
			if name == "commit" && tx.commits != 1 {
				t.Fatal("commit not attempted")
			}
		})
	}
}

func TestTransactionDatabase(t *testing.T) {
	dsn := os.Getenv("SANKA_GO_TRANSACTION_DSN")
	if dsn == "" {
		t.Skip("requires isolated PostgreSQL fixture")
	}
	ctx, stop := context.WithTimeout(context.Background(), 30*time.Second)
	defer stop()
	config, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		t.Fatal("invalid test database configuration")
	}
	config.MaxConns = 1
	pool, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		t.Fatal("test database unavailable")
	}
	defer pool.Close()
	observations := []map[string]any{}
	for _, name := range []string{"commit", "work_error", "constraint", "panic", "commit_error",
		"cancel_before", "cancel_after", "deadline_after"} {
		if _, err := pool.Exec(ctx, "DELETE FROM widgets"); err != nil {
			t.Fatal(err)
		}
		workCtx, cancel := context.WithCancel(ctx)
		if name == "cancel_before" {
			cancel()
		}
		if name == "deadline_after" {
			cancel()
			workCtx, cancel = context.WithDeadline(ctx, time.Now().Add(-time.Second))
		}
		// Start with a live context for the deadline-after-write case.
		beginCtx := workCtx
		if name == "deadline_after" {
			beginCtx = ctx
		}
		calls := 0
		prepared := false
		workFailure := errors.New("operation rejected")
		var err error
		var recovered any
		func() {
			defer func() { recovered = recover() }()
			err = pgx.BeginFunc(beginCtx, pool, func(tx pgx.Tx) error {
				calls++
				if name == "commit_error" {
					_, err := tx.Exec(beginCtx, "INSERT INTO commit_probe VALUES (1,2)")
					prepared = err == nil
					return err
				}
				if _, err := tx.Exec(beginCtx,
					"INSERT INTO widgets(name,count,enabled) VALUES ($1,0,false)",
					"first"); err != nil {
					return err
				}
				switch name {
				case "work_error":
					return workFailure
				case "panic":
					panic("operation panic")
				case "cancel_after":
					cancel()
					return workCtx.Err()
				case "deadline_after":
					return workCtx.Err()
				}
				second := "second"
				if name == "constraint" {
					second = "first"
				}
				_, err := tx.Exec(beginCtx,
					"INSERT INTO widgets(name,count,enabled) VALUES ($1,0,false)", second)
				return err
			})
		}()
		cancel()
		if name == "work_error" && !errors.Is(err, workFailure) {
			t.Fatal("work error lost")
		}
		if name == "constraint" || name == "commit_error" {
			var databaseError *pgconn.PgError
			code := "23505"
			if name == "commit_error" {
				code = "23503"
				if !prepared {
					t.Fatal("expected failure at commit, not during work")
				}
			}
			if !errors.As(err, &databaseError) || databaseError.Code != code {
				t.Fatal("database error classification lost")
			}
		}
		if name == "cancel_before" {
			if calls != 0 || !errors.Is(err, context.Canceled) {
				t.Fatal("cancelled request executed work")
			}
		} else if calls != 1 {
			t.Fatal("work retried or skipped")
		}
		if name == "cancel_after" && !errors.Is(err, context.Canceled) {
			t.Fatal("cancellation lost")
		}
		if name == "deadline_after" && !errors.Is(err, context.DeadlineExceeded) {
			t.Fatal("deadline lost")
		}
		if name == "panic" && recovered != "operation panic" {
			t.Fatal("panic swallowed")
		}
		// MaxConns=1 makes leaked transaction ownership fail this bounded query.
		checkCtx, done := context.WithTimeout(ctx, 3*time.Second)
		rows, queryErr := pool.Query(checkCtx, "SELECT name FROM widgets ORDER BY name")
		if queryErr != nil {
			done()
			t.Fatal("pool unavailable after transaction")
		}
		names, queryErr := pgx.CollectRows(rows, pgx.RowTo[string])
		if queryErr != nil {
			done()
			t.Fatal(queryErr)
		}
		if names == nil {
			names = []string{}
		}
		expected := 0
		if name == "commit" {
			expected = 2
		}
		if len(names) != expected || ((err == nil && recovered == nil) != (name == "commit")) {
			done()
			t.Fatal("incorrect transaction outcome", name)
		}
		var committed int
		queryErr = pool.QueryRow(checkCtx, "SELECT count(*) FROM commit_probe").Scan(&committed)
		if queryErr != nil || committed != 0 {
			done()
			t.Fatal("failed commit persisted")
		}
		if _, err := pool.Exec(checkCtx,
			"INSERT INTO widgets(name,count,enabled) VALUES ('recovery',0,false)"); err != nil {
			done()
			t.Fatal("connection not reusable")
		}
		done()
		observations = append(observations, map[string]any{
			"case": name, "ok": err == nil && recovered == nil, "rows": names,
		})
	}
	content, err := json.Marshal(observations)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile("transactions.json", content, 0600); err != nil {
		t.Fatal(err)
	}
}
"""

SOURCE_TRANSACTIONS = r"""
import contextlib, json, os, sys
framework = sys.argv[1]
dsn = os.environ["SOURCE_TRANSACTION_DSN"]
if framework == "drf":
    from urllib.parse import urlsplit, parse_qs, unquote
    from django.conf import settings
    url = urlsplit(dsn)
    settings.configure(INSTALLED_APPS=[], DATABASES={"default": {
        "ENGINE": "django.db.backends.postgresql", "NAME": url.path.lstrip("/"),
        "USER": unquote(url.username or ""), "PASSWORD": unquote(url.password or ""),
        "HOST": url.hostname, "PORT": url.port,
        "OPTIONS": {"options": parse_qs(url.query)["options"][0]}}})
    import django
    django.setup()
    from django.db import connection, transaction, IntegrityError
    @contextlib.contextmanager
    def unit():
        with transaction.atomic(), connection.cursor() as cursor:
            yield cursor.execute
    def rows(sql):
        with connection.cursor() as cursor:
            cursor.execute(sql)
            return cursor.fetchall() if cursor.description else []
else:
    from sqlalchemy import create_engine
    from sqlalchemy.exc import IntegrityError
    engine = create_engine(dsn.replace("postgresql://", "postgresql+psycopg://", 1),
                           pool_size=1, max_overflow=0, pool_timeout=3)
    @contextlib.contextmanager
    def unit():
        with engine.begin() as connection:
            yield connection.exec_driver_sql
    def rows(sql):
        with engine.begin() as connection:
            result = connection.exec_driver_sql(sql)
            return result.fetchall() if result.returns_rows else []
observations = []
for name in ("commit", "work_error", "constraint", "panic", "commit_error"):
    rows("DELETE FROM widgets")
    ok = False
    prepared = False
    try:
        with unit() as execute:
            if name == "commit_error":
                execute("INSERT INTO commit_probe VALUES (1,2)")
            else:
                execute("INSERT INTO widgets(name,count,enabled) VALUES ('first',0,false)")
                if name in ("work_error", "panic"):
                    raise RuntimeError("operation rejected")
                second = "first" if name == "constraint" else "second"
                execute("INSERT INTO widgets(name,count,enabled) VALUES (%s,0,false)", (second,))
            prepared = True
        ok = True
    except Exception as error:
        if name in ("work_error", "panic"):
            assert type(error) is RuntimeError
        else:
            assert isinstance(error, IntegrityError)
    if name == "commit_error":
        assert prepared, "constraint must fail at commit, not during work"
    saved = [row[0] for row in rows("SELECT name FROM widgets ORDER BY name")]
    assert ok is (name == "commit")
    assert saved == (["first", "second"] if ok else [])
    assert rows("SELECT count(*) FROM commit_probe") == [(0,)]
    rows("INSERT INTO widgets(name,count,enabled) VALUES ('recovery',0,false)")
    observations.append({"case":name, "ok":ok, "rows":saved})
if framework == "drf":
    connection.close()
else:
    engine.dispose()
print(json.dumps(observations))
"""


def run_go(output: Path, test: str, dsn: str = "") -> None:
    (output / "transaction_contract_test.go").write_text(GO_TEST)
    result = subprocess.run(
        ["go", "test", "-mod=readonly", "-count=1", "-p=2", "-timeout=45s", "-run", test, "."],
        cwd=output,
        env=os.environ
        | {
            "GOTOOLCHAIN": "local",
            "GOWORK": "off",
            "GOMAXPROCS": "2",
            "SANKA_GO_TRANSACTION_DSN": dsn,
        },
        text=True,
        capture_output=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        "PostgreSQL transaction qualification failed" if dsn else result.stdout + result.stderr
    )


@pytest.mark.skipif(os.getenv("SANKA_GO_TESTS") != "1", reason="requires qualified Go toolchain")
def test_native_transaction_faults(tmp_path: Path) -> None:
    output = generate(tmp_path, "flask", "fiber")
    run_go(output, "TestTransactionFaults")


@pytest.mark.skipif(
    os.getenv("SANKA_GO_TESTS") != "1" or not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN"),
    reason="requires explicit PostgreSQL fixture and Go toolchain",
)
@pytest.mark.parametrize("framework", ["drf", "flask", "fastapi"])
def test_transaction_database_parity(tmp_path: Path, framework: str) -> None:
    import psycopg
    from psycopg import sql

    output = generate(tmp_path, framework, "fiber")
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    schemas = ["go_transaction_" + uuid.uuid4().hex for _ in range(2)]
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            for schema in schemas:
                admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            source_dsn, target_dsn = [schema_dsn(dsn, schema) for schema in schemas]
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    SOURCE_DDL,
                    framework,
                    str(tmp_path / "models.py"),
                    source_dsn,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, "source fixture schema failed"
            result = subprocess.run(
                ["go", "run", "-mod=readonly", "./cmd/migrate", "up"],
                cwd=output,
                env=os.environ
                | {
                    "DATABASE_URL": target_dsn,
                    "GOTOOLCHAIN": "local",
                    "GOWORK": "off",
                    "GOMAXPROCS": "2",
                },
                capture_output=True,
                text=True,
                timeout=180,
            )
            assert result.returncode == 0, "target fixture schema failed"
            for url in (source_dsn, target_dsn):
                with psycopg.connect(url, autocommit=True) as connection:
                    connection.execute(
                        "CREATE TABLE commit_probe (id integer PRIMARY KEY, "
                        "ref integer REFERENCES commit_probe(id) "
                        "DEFERRABLE INITIALLY DEFERRED)"
                    )
            source = subprocess.run(
                [sys.executable, "-I", "-c", SOURCE_TRANSACTIONS, framework],
                env=os.environ | {"SOURCE_TRANSACTION_DSN": source_dsn},
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert source.returncode == 0, "source transaction qualification failed"
            run_go(output, "TestTransactionDatabase", target_dsn)
            observed = json.loads((output / "transactions.json").read_text())
            assert observed[:5] == json.loads(source.stdout)
            assert all(not case["ok"] and not case["rows"] for case in observed[5:])
        finally:
            for schema in schemas:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )

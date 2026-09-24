# SPDX-License-Identifier: Apache-2.0
"""One-time, opt-in PostgreSQL row transfer for captured Django tables."""

TRANSFER_SCRIPT = '''# SPDX-License-Identifier: Apache-2.0
"""Copy only contract tables between isolated PostgreSQL schemas.

Requires psycopg 3 in the invoking Python environment. Supply connection URLs
through SANKA_GO_SOURCE_DATABASE_URL and DATABASE_URL; --execute writes the target.
"""
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

import psycopg
from psycopg import sql

CONTRACT = json.loads((Path(__file__).resolve().parents[1] / "contract.json").read_text())
MODELS = CONTRACT["models"]


def fail(message):
    raise ValueError("transfer refused: " + message)


def identity(connection):
    return connection.execute(
        "SELECT extract(epoch from pg_postmaster_start_time())::text, "
        "current_database(), current_schema()"
    ).fetchone()


def column_signature(connection, table):
    return connection.execute(
        "SELECT column_name,data_type,is_nullable,character_maximum_length, "
        "CASE WHEN data_type='numeric' THEN numeric_precision END, "
        "CASE WHEN data_type='numeric' THEN numeric_scale END "
        "FROM information_schema.columns WHERE table_schema=current_schema() "
        "AND table_name=%s ORDER BY ordinal_position", (table,)
    ).fetchall()


def constraint_signature(connection, table):
    return connection.execute(
        "SELECT con.contype::text, "
        "CASE WHEN con.contype='f' THEN "
        "(SELECT jsonb_agg(a.attname ORDER BY x.ordinality)::text "
        " FROM unnest(con.conkey) WITH ORDINALITY x(num,ordinality) "
        " JOIN pg_attribute a ON a.attrelid=con.conrelid AND a.attnum=x.num) "
        "ELSE pg_get_constraintdef(con.oid,true) END, "
        "CASE WHEN con.contype='f' THEN "
        "(SELECT jsonb_agg(a.attname ORDER BY x.ordinality)::text "
        " FROM unnest(con.confkey) WITH ORDINALITY x(num,ordinality) "
        " JOIN pg_attribute a ON a.attrelid=con.confrelid AND a.attnum=x.num) "
        "ELSE NULL END, "
        "CASE WHEN con.contype='f' THEN (SELECT relname FROM pg_class WHERE oid=con.confrelid) "
        "ELSE NULL END "
        "FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relname=%s "
        "ORDER BY 1,2,3,4", (table,)
    ).fetchall()


def index_signature(connection, table):
    return connection.execute(
        "SELECT x.indisunique,x.indisprimary,am.amname, "
        "(SELECT jsonb_agg(a.attname ORDER BY k.ordinality)::text "
        " FROM unnest(x.indkey) WITH ORDINALITY k(num,ordinality) "
        " JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.num), "
        "x.indpred IS NULL,x.indexprs IS NULL "
        "FROM pg_index x JOIN pg_class t ON t.oid=x.indrelid "
        "JOIN pg_namespace n ON n.oid=t.relnamespace "
        "JOIN pg_class i ON i.oid=x.indexrelid JOIN pg_am am ON am.oid=i.relam "
        "WHERE n.nspname=current_schema() AND t.relname=%s ORDER BY 1,2,3,4,5,6", (table,)
    ).fetchall()


def sequence(connection, table):
    name = connection.execute(
        "SELECT pg_get_serial_sequence(format('%%I.%%I',current_schema(),%s::text),'id')", (table,)
    ).fetchone()[0]
    if not name:
        fail(table + " has no owned id sequence")
    state = connection.execute(
        sql.SQL("SELECT last_value,is_called FROM {}").format(
            sql.Identifier(*name.split("."))
        )
    ).fetchone()
    return name, state


def schema_check(source, target):
    expected = {model["table"] for model in MODELS}
    actual = {row[0] for row in target.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relkind IN ('r','p','v','m','f')"
    )}
    if actual != expected | {"goose_db_version"}:
        fail("target table set differs from the generated baseline")
    applied = target.execute(
        "SELECT EXISTS(SELECT 1 FROM goose_db_version WHERE version_id=1 AND is_applied)"
    ).fetchone()[0]
    if not applied:
        fail("target baseline migration is not applied")
    for model in MODELS:
        table = model["table"]
        left, right = column_signature(source, table), column_signature(target, table)
        if left != right or [row[0] for row in right] != [f["name"] for f in model["fields"]]:
            fail(table + " columns differ from the captured schema")
        if constraint_signature(source, table) != constraint_signature(target, table):
            fail(table + " constraints differ")
        if Counter(index_signature(target, table)) - Counter(index_signature(source, table)):
            fail(table + " required indexes differ")
        for connection in (source, target):
            protected = connection.execute(
                "SELECT c.relrowsecurity OR c.relforcerowsecurity OR EXISTS ("
                "SELECT 1 FROM pg_trigger g WHERE g.tgrelid=c.oid AND NOT g.tgisinternal) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=current_schema() AND c.relname=%s", (table,)
            ).fetchone()
            if not protected or protected[0]:
                fail(table + " has unsupported triggers or row security")
        sequence(source, table)
        sequence(target, table)


def excluded_tables(source):
    captured = {model["table"] for model in MODELS}
    tables = {row[0] for row in source.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relkind IN ('r','p')"
    )}
    schema = source.execute("SELECT current_schema()").fetchone()[0]
    for child_schema, child, parent_schema, parent in source.execute(
        "SELECT cn.nspname, c.relname, pn.nspname, p.relname "
        "FROM pg_constraint fk "
        "JOIN pg_class c ON c.oid=fk.conrelid "
        "JOIN pg_namespace cn ON cn.oid=c.relnamespace "
        "JOIN pg_class p ON p.oid=fk.confrelid "
        "JOIN pg_namespace pn ON pn.oid=p.relnamespace "
        "WHERE fk.contype='f' AND (cn.nspname=current_schema() "
        "OR pn.nspname=current_schema())"
    ):
        if (child_schema == schema and child in captured) != (
            parent_schema == schema and parent in captured
        ):
            fail("external foreign key connects captured and excluded tables: "
                 + child + " -> " + parent)
    return sorted(tables - captured)


def rows(connection, model):
    table = model["table"]
    columns = [f["name"] for f in model["fields"]]
    query = sql.SQL("SELECT {} FROM {} ORDER BY id").format(
        sql.SQL(",").join(map(sql.Identifier, columns)), sql.Identifier(table)
    )
    cursor = connection.cursor(name="sanka_copy_" + table)
    cursor.execute(query)
    try:
        while batch := cursor.fetchmany(500):
            yield batch
    finally:
        cursor.close()


def fingerprint(connection, model):
    digest = hashlib.sha256()
    count = 0
    for batch in rows(connection, model):
        for row in batch:
            digest.update(json.dumps(row, default=str, ensure_ascii=False).encode() + b"\\n")
            count += 1
    return count, digest.hexdigest()


def transfer(mode, acknowledge_excluded=False):
    source_url = os.environ.get("SANKA_GO_SOURCE_DATABASE_URL")
    target_url = os.environ.get("DATABASE_URL")
    if not source_url or not target_url:
        fail("SANKA_GO_SOURCE_DATABASE_URL and DATABASE_URL are required")
    with psycopg.connect(source_url) as source, psycopg.connect(target_url) as target:
        if identity(source) == identity(target):
            fail("source and target identify the same database schema")
        for model in MODELS:
            table = model["table"]
            if mode != "dry-run":
                source.execute(sql.SQL("LOCK TABLE {} IN SHARE MODE").format(sql.Identifier(table)))
                target.execute(
                    sql.SQL("LOCK TABLE {} IN {} MODE").format(
                        sql.Identifier(table),
                        sql.SQL("ACCESS EXCLUSIVE" if mode == "execute" else "SHARE"),
                    )
                )
        schema_check(source, target)
        excluded = excluded_tables(source)
        counts = {}
        for model in MODELS:
            table = model["table"]
            if mode != "verify":
                if target.execute(sql.SQL("SELECT EXISTS(SELECT 1 FROM {})").format(
                    sql.Identifier(table)
                )).fetchone()[0]:
                    fail("target is nonempty: " + table)
            counts[table] = source.execute(sql.SQL("SELECT count(*) FROM {}").format(
                sql.Identifier(table)
            )).fetchone()[0]
        if mode == "verify":
            for model in MODELS:
                table = model["table"]
                if fingerprint(source, model) != fingerprint(target, model):
                    fail(table + " row snapshot differs after copy")
                if sequence(source, table)[1] != sequence(target, table)[1]:
                    fail(table + " sequence differs after copy")
            target.rollback()
            source.rollback()
            return {"mode": "verified", "rows": counts, "excluded_tables": excluded}
        if mode == "dry-run":
            target.rollback()
            source.rollback()
            return {"mode": "dry-run", "rows": counts, "excluded_tables": excluded}
        if excluded and not acknowledge_excluded:
            fail("source has excluded tables; review the dry run and use "
                 "--acknowledge-excluded-tables")
        for model in MODELS:
            table = model["table"]
            columns = [f["name"] for f in model["fields"]]
            statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, columns)),
                sql.SQL(",").join(sql.Placeholder() for _ in columns)
            )
            with target.cursor() as destination:
                for batch in rows(source, model):
                    destination.executemany(statement, batch)
            if fingerprint(source, model) != fingerprint(target, model):
                fail(table + " row snapshot differs after copy")
            source_sequence = sequence(source, table)[1]
            target.execute("SELECT setval(%s,%s,%s)",
                           (sequence(target, table)[0], *source_sequence))
            if sequence(target, table)[1] != source_sequence:
                fail(table + " sequence differs after copy")
        target.commit()
        source.rollback()
        return {"mode": "executed", "rows": counts, "excluded_tables": excluded}


if __name__ == "__main__":
    arguments = sys.argv[1:]
    allowed = ([], ["--execute"], ["--execute", "--acknowledge-excluded-tables"],
               ["--verify"])
    if arguments not in allowed:
        raise SystemExit("usage: transfer_existing.py "
                         "[--execute [--acknowledge-excluded-tables] | --verify]")
    try:
        mode = ("verify" if arguments == ["--verify"] else
                "execute" if arguments else "dry-run")
        report = transfer(mode, "--acknowledge-excluded-tables" in arguments)
        print(json.dumps(report, sort_keys=True))
    except psycopg.OperationalError:
        raise SystemExit("transfer refused: database connection failed") from None
    except (ValueError, psycopg.Error) as error:
        raise SystemExit(str(error)) from None
'''

# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""Explicit, transactional transfer from a read-only SQLite source."""

from typing import Any

SQLITE_TRANSFER_SCRIPT = r'''# SPDX-License-Identifier: Apache-2.0
"""Dry run by default. --execute copies only captured tables; --verify reads them back.

SQLite connections use absolute file: URIs. PostgreSQL targets require psycopg 3.
The source is opened read-only. The destination must have the reviewed migrations
fully applied and contain no application rows. No source rows are changed.
"""
import hashlib
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit, unquote

CONTRACT = json.loads((Path(__file__).resolve().parents[1] / "contract.json").read_text())
MODELS = CONTRACT["models"]
SQLITE_TARGET = CONTRACT["configuration"]["database_layer"] == "sqlite"
COUNTERS = "drf_project" in CONTRACT
DRF_SOURCE = CONTRACT["configuration"]["source_framework"] == "drf"
DEFAULTS = {(step["table"], step["column"]): step["server_default"] for revision in CONTRACT.get("fastapi_persistence", {}).get("lowered_migrations", []) for step in revision["steps"] if step["name"] == "add_column" and step["server_default"] is not None}


def fail(message):
    raise ValueError("transfer refused: " + message)


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def sqlite_path(url):
    parsed = urlsplit(url)
    path = Path(unquote(parsed.path))
    if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment or not path.is_absolute():
        fail("SQLite URL must be an absolute file: URI without options")
    if path.is_symlink() or not path.is_file():
        fail("SQLite file must already exist and must not be a symlink")
    return path.resolve()


def primary(model):
    return next(field for field in model["fields"] if field["primary_key"])


def default_matches(actual, expected):
    if actual is None:
        return expected is None
    # Captured Alembic defaults are literals; expressions never qualify.
    value = re.sub(r"::(?:character varying|text|integer|bigint|boolean)$", "", actual).strip("()")
    return expected is not None and value == "'" + expected.replace("'", "''") + "'"


def expected_indexes(model):
    result = Counter((bool(field["unique"]), (field["name"],)) for field in model["fields"] if not field["primary_key"] and field["unique"])
    result.update((False, (field["name"],)) for field in model["fields"] if field.get("index"))
    result.update((True, tuple(item["columns"])) for item in model.get("constraints", []))
    result.update((False, tuple(item["columns"])) for item in model.get("indexes", []))
    return result


def versions(connection):
    rows = connection.execute("SELECT version_id,is_applied FROM goose_db_version ORDER BY id").fetchall()
    latest = {int(version): bool(applied) for version, applied in rows if version > 0}
    expected = set(range(1, len(CONTRACT.get("fastapi_persistence", {}).get("lowered_migrations", []) or [None]) + 1))
    if {version for version, applied in latest.items() if applied} != expected:
        fail("target migrations are not fully applied")


def indexes(connection, table):
    result = Counter()
    for _, name, unique, origin, partial in connection.execute("PRAGMA index_list(" + quote(table) + ")"):
        columns = tuple(row[2] for row in connection.execute("PRAGMA index_info(" + quote(name) + ")"))
        keys = [row for row in connection.execute("PRAGMA index_xinfo(" + quote(name) + ")") if row[5]]
        if partial or any(column is None for column in columns) or any(row[3] or row[4] != "BINARY" for row in keys):
            fail(table + " has unsupported index expressions, ordering or collation")
        if origin != "pk":
            result[(bool(unique), columns)] += 1
    return result


def check_sqlite(connection, model, *, source):
    table = model["table"]
    columns = connection.execute("PRAGMA table_xinfo(" + quote(table) + ")").fetchall()
    if [row[1] for row in columns] != [field["name"] for field in model["fields"]]:
        fail(table + " columns differ from the captured schema")
    for column, field in zip(columns, model["fields"], strict=True):
        _, name, kind, not_null, default, pk, hidden = column
        if hidden:
            fail(table + "." + name + " is an uncaptured generated column")
        integer = field["go_type"] in {"int32", "int64", "bool"}
        if integer != ("INT" in kind.upper() or kind.upper() in {"BOOL", "BOOLEAN"}):
            fail(table + "." + name + " has an unsupported SQLite type")
        if not integer and not any(t in kind.upper() for t in ("CHAR", "TEXT", "CLOB")):
            fail(table + "." + name + " has an unsupported SQLite type")
        if bool(pk) != field["primary_key"] or bool(not_null or pk) != (not field["nullable"]):
            fail(table + "." + name + " nullability or primary key differs")
        if not default_matches(default, DEFAULTS.get((table, name))):
            fail(table + "." + name + " default differs")
    ddl = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
    ddl = re.sub(r"'(?:''|[^'])*'", "''", ddl)
    checks = re.findall(r"\bCHECK\s*\(([^()]*)\)", ddl, re.I)
    expected_checks = [field["name"] + ">=0" for field in model["fields"] if field.get("kind") == "PositiveIntegerField"]
    if len(re.findall(r"\bCHECK\s*\(", ddl, re.I)) != len(checks) or sorted(re.sub(r'[\s"`\[\]]', '', check) for check in checks) != sorted(expected_checks):
        fail(table + " check constraints differ")
    if primary(model)["auto"] and bool(re.search(r"\bAUTOINCREMENT\b", ddl, re.I)) != DRF_SOURCE:
        fail(table + " identity allocation differs")
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)).fetchone():
        fail(table + " has unsupported triggers")
    expected = {(field["name"], field["references"]["table"], field["references"]["column"], field["references"]["on_delete"]) for field in model["fields"] if "references" in field}
    keys = connection.execute("PRAGMA foreign_key_list(" + quote(table) + ")").fetchall()
    if any(row[5] != "NO ACTION" or row[7] != "NONE" for row in keys):
        fail(table + " has unsupported foreign key actions")
    actual = {(row[3], row[2], row[4], "CASCADE" if source and COUNTERS and row[6] == "NO ACTION" and any(f["name"] == row[3] and f.get("references", {}).get("on_delete") == "CASCADE" for f in model["fields"]) else row[6]) for row in keys}
    if actual != expected:
        fail(table + " foreign keys differ")
    deferred = [match[1].upper() == "DEFERRED" for match in re.findall(r"\b((?:NOT\s+)?DEFERRABLE)(?:\s+INITIALLY\s+(DEFERRED|IMMEDIATE))?", ddl, re.I) if not match[0].upper().startswith("NOT")]
    if sorted(deferred) != sorted(field["references"]["deferred"] for field in model["fields"] if field.get("references", {}).get("deferrable")):
        fail(table + " foreign key deferral differs")
    return indexes(connection, table)


def target_check(source, target):
    expected = {model["table"] for model in MODELS} | {"goose_db_version"} | ({"migration_identity"} if COUNTERS else set())
    if SQLITE_TARGET:
        actual = {row[0] for row in target.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'")}
    else:
        actual = {row[0] for row in target.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=current_schema() AND c.relkind IN ('r','p','v','m','f')")}
    if actual != expected:
        fail("target table set differs from the reviewed baseline")
    versions(target)
    for model in MODELS:
        required = expected_indexes(model)
        if check_sqlite(source, model, source=True) != required:
            fail(model["table"] + " source indexes differ from capture")
        if SQLITE_TARGET:
            available = check_sqlite(target, model, source=False)
        else:
            import transfer_postgres as pg
            columns = target.execute("SELECT column_name,data_type,is_nullable,character_maximum_length,column_default,is_generated FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=%s ORDER BY ordinal_position", (model["table"],)).fetchall()
            if [column[0] for column in columns] != [field["name"] for field in model["fields"]]:
                fail(model["table"] + " columns differ from the captured schema")
            for column, field in zip(columns, model["fields"], strict=True):
                kind = field["sql_type"]
                expected_type = "character varying" if kind.startswith("varchar(") else kind
                if column[1] != expected_type or (column[2] == "YES") != field["nullable"]:
                    fail(model["table"] + " destination column type differs")
                if kind.startswith("varchar(") and column[3] != int(kind[8:-1]):
                    fail(model["table"] + " destination text limit differs")
                if column[5] != "NEVER" or (not field["auto"] and not default_matches(column[4], DEFAULTS.get((model["table"], field["name"])))):
                    fail(model["table"] + " has uncaptured generated values or defaults")
            protected = target.execute("SELECT c.relrowsecurity OR c.relforcerowsecurity OR EXISTS(SELECT 1 FROM pg_trigger g WHERE g.tgrelid=c.oid AND NOT g.tgisinternal) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=current_schema() AND c.relname=%s", (model["table"],)).fetchone()
            if not protected or protected[0]:
                fail(model["table"] + " has unsupported triggers or row security")
            expected_keys = {(field["name"], field["references"]["table"], field["references"]["column"], {"NO ACTION": "a", "RESTRICT": "r", "CASCADE": "c", "SET NULL": "n"}[field["references"]["on_delete"]], field["references"]["deferrable"], field["references"]["deferred"]) for field in model["fields"] if "references" in field}
            actual_keys, checks = set(), []
            for row in pg.constraint_signature(target, model):
                if not row[9] or row[0] not in {"p", "u", "f", "c", "n"}:
                    fail(model["table"] + " has unsupported constraints")
                if row[0] in {"p", "u"} and (row[7] or row[8]):
                    fail(model["table"] + " has unsupported deferred uniqueness")
                if row[0] == "f":
                    if row[4] != "a" or row[6] != "s":
                        fail(model["table"] + " has unsupported foreign key actions")
                    actual_keys.add((json.loads(row[1])[0], row[3], json.loads(row[2])[0], row[5], row[7], row[8]))
                elif row[0] == "c":
                    checks.append(re.sub(r'[\s"()]', '', row[1]).removeprefix("CHECK"))
            if actual_keys != expected_keys or sorted(checks) != sorted(field["name"] + ">=0" for field in model["fields"] if field.get("kind") == "PositiveIntegerField"):
                fail(model["table"] + " destination constraints differ")
            if target.execute("SELECT 1 FROM pg_constraint con JOIN pg_class child ON child.oid=con.conrelid JOIN pg_namespace cn ON cn.oid=child.relnamespace JOIN pg_class parent ON parent.oid=con.confrelid JOIN pg_namespace pn ON pn.oid=parent.relnamespace WHERE cn.nspname=current_schema() AND child.relname=%s AND pn.nspname<>current_schema()", (model["table"],)).fetchone():
                fail(model["table"] + " has external foreign keys")
            available = Counter()
            primary_indexes = []
            for unique, pk, method, columns, no_predicate, no_expression in pg.index_signature(target, model["table"]):
                if method != "btree" or not no_predicate or not no_expression:
                    fail(model["table"] + " has unsupported indexes")
                if pk:
                    primary_indexes.append(tuple(json.loads(columns)))
                else:
                    available[(unique, tuple(json.loads(columns)))] += 1
            if primary_indexes != [(primary(model)["name"],)]:
                fail(model["table"] + " primary key differs")
        if available != required:
            fail(model["table"] + " required indexes differ")


def batches(connection, model):
    fields = model["fields"]
    cursor = connection.execute("SELECT " + ','.join(quote(f["name"]) for f in fields) + " FROM " + quote(model["table"]) + " ORDER BY " + quote(primary(model)["name"]))
    try:
        while batch := cursor.fetchmany(500):
            normalized = []
            for row in batch:
                values = []
                for field, value in zip(fields, row, strict=True):
                    if value is None and field["nullable"]:
                        values.append(None)
                        continue
                    kind = field["go_type"]
                    if kind == "bool":
                        if type(value) not in {int, bool} or value not in (0, 1):
                            fail("invalid boolean in " + model["table"])
                        value = bool(value)
                    elif kind in {"int32", "int64"}:
                        bits = 32 if kind == "int32" else 64
                        if type(value) is not int or not -(2 ** (bits - 1)) <= value < 2 ** (bits - 1):
                            fail("out of range integer in " + model["table"])
                    elif kind == "string":
                        if not isinstance(value, str):
                            fail("invalid text in " + model["table"])
                    else:
                        fail("unsupported SQLite value type")
                    values.append(value)
                normalized.append(tuple(values))
            yield normalized
    finally:
        cursor.close()


def fingerprint(connection, model):
    digest, count = hashlib.sha256(), 0
    for batch in batches(connection, model):
        for row in batch:
            digest.update(json.dumps(row, ensure_ascii=False).encode() + b"\n")
            count += 1
    return count, digest.hexdigest()


def highwater(connection, model):
    if not primary(model)["auto"]:
        return None
    if not DRF_SOURCE:
        return connection.execute("SELECT COALESCE(MAX(" + quote(primary(model)["name"]) + "),0) FROM " + quote(model["table"])).fetchone()[0]
    row = connection.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (model["table"],)).fetchone()
    value = row[0] if row else 0
    maximum = connection.execute("SELECT COALESCE(MAX(" + quote(primary(model)["name"]) + "),0) FROM " + quote(model["table"])).fetchone()[0]
    if type(value) is not int or value < maximum or value < 0:
        fail(model["table"] + " identity state differs from its rows")
    return value


def transfer(mode, acknowledge_excluded=False):
    source_path = sqlite_path(os.environ.get("SANKA_GO_SOURCE_DATABASE_URL", ""))
    target_url = os.environ.get("DATABASE_URL", "")
    if SQLITE_TARGET:
        target_path = sqlite_path(target_url)
        if os.path.samefile(source_path, target_path):
            fail("source and target identify the same SQLite file")
        target = sqlite3.connect(target_path.as_uri() + "?mode=rw", uri=True, isolation_level=None)
        target.execute("PRAGMA foreign_keys=ON")
    else:
        import psycopg
        parsed = urlsplit(target_url)
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or parsed.path in {"", "/"} or parsed.fragment:
            fail("PostgreSQL URL must specify a host and database")
        target = psycopg.connect(target_url)
    source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    try:
        source.execute("BEGIN")
        if SQLITE_TARGET:
            target.execute("BEGIN IMMEDIATE" if mode == "execute" else "BEGIN")
        else:
            target.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            for model in MODELS:
                target.execute("LOCK TABLE " + quote(model["table"]) + (" IN ACCESS EXCLUSIVE MODE" if mode == "execute" else " IN SHARE MODE"))
        target_check(source, target)
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        captured = {model["table"] for model in MODELS}
        for table in tables:
            for fk in source.execute("PRAGMA foreign_key_list(" + quote(table) + ")"):
                if (table in captured) != (fk[2] in captured):
                    fail("external foreign key connects captured and excluded tables")
        excluded = sorted(tables - captured)
        counts = {}
        for model in MODELS:
            table = model["table"]
            counts[table] = fingerprint(source, model)[0]
            if mode != "verify" and target.execute("SELECT 1 FROM " + quote(table) + " LIMIT 1").fetchone():
                fail("target is nonempty: " + table)
            if mode == "verify" and fingerprint(source, model) != fingerprint(target, model):
                fail(table + " row snapshot differs after copy")
            if COUNTERS and mode == "verify":
                marker = "?" if SQLITE_TARGET else "%s"
                value = target.execute("SELECT value FROM migration_identity WHERE table_name=" + marker, (table,)).fetchone()
                if not value or value[0] != highwater(source, model):
                    fail(table + " identity differs after copy")
            if DRF_SOURCE and SQLITE_TARGET and mode == "verify" and highwater(target, model) != highwater(source, model):
                fail(table + " SQLite identity differs after copy")
        if mode == "execute":
            if excluded and not acknowledge_excluded:
                fail("review excluded tables and use --acknowledge-excluded-tables")
            for model in MODELS:
                fields, table = model["fields"], model["table"]
                marker = "?" if SQLITE_TARGET else "%s"
                query = "INSERT INTO " + quote(table) + " (" + ','.join(quote(f["name"]) for f in fields) + ") VALUES (" + ','.join(marker for _ in fields) + ")"
                with closing(target.cursor()) as cursor:
                    for batch in batches(source, model):
                        cursor.executemany(query, batch)
                if fingerprint(source, model) != fingerprint(target, model):
                    fail(table + " row snapshot differs after copy")
                if DRF_SOURCE:
                    value = highwater(source, model)
                    if COUNTERS:
                        target.execute("UPDATE migration_identity SET value=" + marker + " WHERE table_name=" + marker, (value, table))
                    if SQLITE_TARGET:
                        target.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
                        target.execute("INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)", (table, value))
            target.commit()
        else:
            target.rollback()
        return {"mode": {"execute": "executed", "verify": "verified"}.get(mode, mode), "rows": counts, "excluded_tables": excluded}
    finally:
        source.rollback()
        source.close()
        target.close()


if __name__ == "__main__":
    arguments = sys.argv[1:]
    allowed = ([], ["--execute"], ["--execute", "--acknowledge-excluded-tables"], ["--verify"])
    if arguments not in allowed:
        raise SystemExit("usage: transfer_existing.py [--execute [--acknowledge-excluded-tables] | --verify]")
    try:
        print(json.dumps(transfer("verify" if arguments == ["--verify"] else "execute" if arguments else "dry-run", "--acknowledge-excluded-tables" in arguments), sort_keys=True))
    except Exception as error:
        # Connection errors may include URLs/passwords; schema failures are our own messages.
        raise SystemExit(str(error) if isinstance(error, ValueError) else "transfer refused: database operation failed; target transaction rolled back") from None
'''


def render_transfer(captured: dict[str, Any]) -> dict[str, str]:
    from .drf_transfer import TRANSFER_SCRIPT

    source_sqlite = (
        captured.get("drf_project", {}).get("database", {}).get("engine") == "sqlite"
        or captured["configuration"].get("source_database") == "sqlite"
    )
    if not source_sqlite:
        return {"tools/transfer_existing.py": TRANSFER_SCRIPT}
    result = {"tools/transfer_existing.py": SQLITE_TRANSFER_SCRIPT}
    if captured["configuration"]["database_layer"] == "pgx":
        result["tools/transfer_postgres.py"] = TRANSFER_SCRIPT
    return result

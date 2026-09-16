# SPDX-License-Identifier: Apache-2.0
"""Emit standalone SQLAlchemy schema and explicit Alembic migration ownership."""

from __future__ import annotations

import hashlib
import json
import keyword
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sanka_code_migration.drf.models import order_tables, unsafe_table_name


def _type(column: dict[str, Any]) -> str:
    kind = column["kind"]
    if kind == "string":
        return f"sa.String({column['length']!r})"
    if kind == "decimal":
        return f"sa.Numeric({column['precision']}, {column['scale']})"
    if kind == "datetime":
        return "sa.DateTime(timezone=True)"
    if kind == "uuid":
        return "sa.Uuid()"
    names = {
        "integer": "Integer",
        "big_integer": "BigInteger",
        "small_integer": "SmallInteger",
        "text": "Text",
        "boolean": "Boolean",
        "float": "Float",
        "date": "Date",
        "time": "Time",
        "binary": "LargeBinary",
    }
    value = "sa." + names[kind] + "()"
    if column["autoincrement"]:
        value += ".with_variant(sa.Integer(), 'sqlite')"
    return value


def _column(column: dict[str, Any], *, defaults: bool) -> str:
    args = [repr(column["name"]), _type(column)]
    reference = column.get("references")
    if reference:
        # Django implements on_delete in Python, not as a database ON DELETE clause.
        target = reference["table"] + "." + reference["column"]
        args.append(
            f"sa.ForeignKey({target!r}, deferrable={reference.get('deferrable', True)!r}, "
            f"initially={reference.get('initially', 'DEFERRED')!r})"
        )
    args += [f"primary_key={column['primary_key']!r}", f"nullable={column['nullable']!r}"]
    if column["unique"] and not column["primary_key"]:
        args.append("unique=True")
    if column["autoincrement"]:
        args.append("autoincrement=True")
    default = column.get("default")
    automatic = column.get("auto_now") or column.get("auto_now_add")
    if defaults and automatic:
        expression = "source_today" if column["kind"] == "date" else "source_now"
        args.append("default=" + expression)
        if column.get("auto_now"):
            args.append("onupdate=" + expression)
    elif defaults and default:
        if "factory" in default:
            expression = {"uuid4": "uuid.uuid4", "now": "source_now"}[default["factory"]]
        elif "decimal" in default:
            expression = f"Decimal({default['decimal']!r})"
        elif "uuid" in default:
            expression = f"uuid.UUID({default['uuid']!r})"
        elif "temporal" in default:
            expression = f"datetime.{column['kind']}.fromisoformat({default['temporal']!r})"
        else:
            expression = repr(default["value"])
        args.append("default=" + expression)
    return "sa.Column(" + ", ".join(args) + ")"


def _schema_hash(schema: dict[str, Any]) -> str:
    payload = {key: value for key, value in schema.items() if key != "schema_hash"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _validated_tables(schema: dict[str, Any]) -> list[dict[str, Any]]:
    if schema.get("schema_hash") != _schema_hash(schema):
        raise ValueError("schema hash does not match the reviewed schema")
    if type(schema.get("use_tz")) is not bool:
        raise ValueError("native database requires an explicit source USE_TZ setting")
    for setting in ("timezone", "connection_timezone"):
        if type(schema.get(setting)) is not str or not schema[setting]:
            raise ValueError(f"native database requires an explicit source {setting}")
        try:
            ZoneInfo(schema[setting])
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"source {setting} is not an IANA timezone") from error
    names = {table["name"] for table in schema["tables"]}
    if len(names) != len(schema["tables"]):
        raise ValueError("native database has duplicate table names")
    for table in schema["tables"]:
        if unsafe_table_name(table["name"]):
            raise ValueError("schema-qualified or quoted table names are unsupported")
        for column in table["columns"]:
            if (column.get("auto_now") or column.get("auto_now_add")) and column["kind"] not in {
                "date",
                "datetime",
            }:
                raise ValueError("auto_now and auto_now_add require date or datetime columns")
            if column.get("auto_now") and column.get("auto_now_add"):
                raise ValueError("auto_now and auto_now_add cannot both be enabled")
            reference = column.get("references")
            if not reference:
                continue
            if reference["table"] not in names:
                raise ValueError("foreign key references a table outside the captured schema")
            if reference.get("on_delete") not in {
                "CASCADE",
                "PROTECT",
                "RESTRICT",
                "SET_NULL",
                "DO_NOTHING",
            }:
                raise ValueError("on_delete application semantics are unsupported")
            if reference.get("on_delete") == "SET_NULL" and not column["nullable"]:
                raise ValueError("SET_NULL requires a nullable foreign key")
    ordered, cycle = order_tables(schema["tables"])
    if cycle:
        raise ValueError("foreign-key cycle requires deferred constraint support")
    return ordered


def render_database(schema: dict[str, Any], *, module_prefix: str = "") -> dict[str, str]:
    """Return reviewed files; never connect to or modify a database."""
    if module_prefix and (not module_prefix.isidentifier() or keyword.iskeyword(module_prefix)):
        raise ValueError("module prefix must be one Python package name")
    if schema["gaps"]:
        raise ValueError("native database generation has unresolved schema gaps")
    if schema["dialect"] not in {"sqlite", "postgresql"}:
        raise ValueError("native database requires SQLite or PostgreSQL")
    tables = _validated_tables(schema)
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"), allow_nan=False)
    revision = hashlib.sha256(canonical.encode()).hexdigest()[:20]
    models = [
        "# Generated by Sanka. Application-owned schema; no source framework imports.",
        "import datetime",
        "import uuid",
        "from decimal import Decimal",
        "from zoneinfo import ZoneInfo",
        "import sqlalchemy as sa",
        "",
        f"USE_TZ = {schema['use_tz']!r}",
        f"TIME_ZONE = ZoneInfo({schema['timezone']!r})",
        "",
        "def source_now():",
        "    if USE_TZ:",
        "        return datetime.datetime.now(datetime.timezone.utc)",
        "    return datetime.datetime.now(TIME_ZONE).replace(tzinfo=None)",
        "",
        "def source_today():",
        "    return datetime.date.today()",
        "",
        "metadata = sa.MetaData()",
        "TABLES = {}",
        "",
    ]
    migration = [
        "# Generated by Sanka. Immutable initial schema snapshot.",
        "import sqlalchemy as sa",
        "from alembic import op",
        "",
        f"revision = {revision!r}",
        "down_revision = None",
        "branch_labels = None",
        "depends_on = None",
        "",
        "def upgrade():",
        "    existing = set(sa.inspect(op.get_bind()).get_table_names()) - {'alembic_version'}",
        "    if existing:",
        (
            "        raise RuntimeError('Initial migration requires an empty database; "
            "use reviewed adoption')"
        ),
    ]
    for table in tables:
        name = table["name"]
        sqlite_autoincrement = schema["dialect"] == "sqlite" and any(
            column["primary_key"] and column["autoincrement"] for column in table["columns"]
        )
        model_args = [_column(c, defaults=True) for c in table["columns"]]
        migration_args = [_column(c, defaults=False) for c in table["columns"]]
        constraints = []
        for constraint in table["unique_constraints"]:
            fields = ", ".join(repr(c) for c in constraint["columns"])
            constraints.append(f"sa.UniqueConstraint({fields}, name={constraint['name']!r})")
        for column in table["columns"]:
            if column["positive"]:
                quoted = '"' + column["name"].replace('"', '""') + '"'
                constraints.append(f"sa.CheckConstraint({(quoted + ' >= 0')!r})")
        models.append(f"TABLES[{name!r}] = sa.Table({name!r}, metadata,")
        models.extend("    " + arg + "," for arg in [*model_args, *constraints])
        if sqlite_autoincrement:
            models.append("    sqlite_autoincrement=True,")
        models.append(")")
        migration.append(f"    op.create_table({name!r},")
        migration.extend("        " + arg + "," for arg in [*migration_args, *constraints])
        if sqlite_autoincrement:
            migration.append("        sqlite_autoincrement=True,")
        migration.append("    )")
        indexes = list(table["indexes"])
        for column in table["columns"]:
            if column["indexed"] and not column["unique"]:
                digest = hashlib.sha256((name + ":" + column["name"]).encode()).hexdigest()[:12]
                indexes.append(
                    {
                        "name": column.get("index_name") or "ix_" + digest,
                        "columns": [column["name"]],
                        "descending": [False],
                    }
                )
        for index in indexes:
            expressions = [
                f"TABLES[{name!r}].c[{col!r}]" + (".desc()" if descending else "")
                for col, descending in zip(index["columns"], index["descending"], strict=True)
            ]
            models.append(f"sa.Index({index['name']!r}, {', '.join(expressions)})")
            # Use the frozen SQL expression for descending indexes, never source text.
            index_fields = [
                f"sa.column({col!r}).desc()" if descending else repr(col)
                for col, descending in zip(index["columns"], index["descending"], strict=True)
            ]
            migration.append(
                f"    op.create_index({index['name']!r}, {name!r}, [{', '.join(index_fields)}])"
            )
    if not tables:
        migration.append("    pass")
    migration.extend(
        [
            "",
            "def downgrade():",
            (
                "    raise RuntimeError('Destructive baseline downgrade requires a reviewed "
                "rollback plan')"
            ),
        ]
    )
    path_prefix = module_prefix + "/" if module_prefix else ""
    models_module = module_prefix + ".models" if module_prefix else "models"
    database_module = module_prefix + ".database" if module_prefix else "database"
    files = {
        path_prefix + "models.py": "\n".join(models) + "\n",
        path_prefix + "database.py": _DATABASE.replace(
            "__SANKA_SCHEMA_HASH__", repr(schema["schema_hash"])
        )
        .replace("__SANKA_REVISION__", repr(revision))
        .replace("__SANKA_DIALECT__", repr(schema["dialect"]))
        .replace("__SANKA_CONNECTION_TIMEZONE__", repr(schema["connection_timezone"]))
        .replace("__SANKA_MODELS_MODULE__", models_module),
        "alembic.ini": "[alembic]\nscript_location = %(here)s/migrations\nprepend_sys_path = .\n",
        "migrations/env.py": _ENV.replace("__SANKA_DATABASE_MODULE__", database_module).replace(
            "__SANKA_MODELS_MODULE__", models_module
        ),
        f"migrations/versions/{revision}_initial.py": "\n".join(migration) + "\n",
        path_prefix + "schema.json": json.dumps(schema, sort_keys=True, indent=2) + "\n",
    }
    if module_prefix:
        files[module_prefix + "/__init__.py"] = "# Generated by Sanka.\n"
    return files


_ENV = """# Generated by Sanka. Migrations run only through an explicit Alembic command.
from alembic import context
from __SANKA_DATABASE_MODULE__ import make_engine
from __SANKA_MODELS_MODULE__ import metadata

engine = make_engine()
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
"""

_DATABASE = '''# Generated by Sanka. No automatic schema creation or migration.
from contextlib import nullcontext
import os
import re
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from __SANKA_MODELS_MODULE__ import metadata

EXPECTED_SCHEMA_HASH = __SANKA_SCHEMA_HASH__
EXPECTED_DIALECT = __SANKA_DIALECT__
EXPECTED_CONNECTION_TIMEZONE = __SANKA_CONNECTION_TIMEZONE__
REVISION = __SANKA_REVISION__
_ALLOWED_EXTRA_TABLES = {"alembic_version", "django_migrations"}

def make_engine(url=None):
    url = url or os.environ.get("SANKA_DATABASE_URL")
    if not url:
        raise RuntimeError("SANKA_DATABASE_URL is required")
    parsed = sa.engine.make_url(url)
    if parsed.get_backend_name() != EXPECTED_DIALECT:
        raise ValueError("Database URL does not match the reviewed schema dialect")
    if parsed.get_backend_name() == "postgresql" and parsed.drivername != "postgresql+psycopg":
        parsed = parsed.set(drivername="postgresql+psycopg")
    engine = sa.create_engine(parsed, pool_pre_ping=True)
    if parsed.get_backend_name() == "sqlite":
        @sa.event.listens_for(engine, "connect")
        def foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
    elif parsed.get_backend_name() == "postgresql":
        @sa.event.listens_for(engine, "connect")
        def source_timezone(connection, _record):
            _set_connection_timezone(connection)
    return engine

def _set_connection_timezone(connection):
    previous_autocommit = connection.autocommit
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('TimeZone', %s, false)",
                (EXPECTED_CONNECTION_TIMEZONE,),
            )
    finally:
        connection.autocommit = previous_autocommit

def sessions(engine):
    # Each unit of work owns its Session; callers explicitly own commit/rollback.
    return sessionmaker(engine, expire_on_commit=False)

def _connection(bind):
    return bind.connect() if hasattr(bind, "connect") else nullcontext(bind)

def _matches_autoincrement_default(bind, table_name, column, found):
    if (bind.dialect.name != "postgresql" or not column.primary_key
            or column.autoincrement is not True or found.get("autoincrement") is not True
            or found.get("identity") is not None):
        return False
    default = str(found.get("default") or "").strip()
    match = re.fullmatch(r"nextval\\('((?:[^']|'')+)'::regclass\\)", default)
    if not match:
        return False
    sequence = match.group(1).replace("''", "'")
    statement = sa.text(
        "SELECT to_regclass(:sequence) = "
        "to_regclass(pg_get_serial_sequence(:table_name, :column_name))"
    )
    with _connection(bind) as connection:
        return bool(connection.scalar(statement, {
            "sequence": sequence, "table_name": table_name, "column_name": column.name,
        }))

def _named_constraints_match(actual, expected):
    unmatched = list(actual)
    for expected_name, expected_payload in expected:
        candidate = next((item for item in unmatched
                          if item[1] == expected_payload
                          and (expected_name is None or item[0] == expected_name)), None)
        if candidate is None:
            return False
        unmatched.remove(candidate)
    return not unmatched

def _sqlite_index_directions(bind, index):
    quote = bind.dialect.identifier_preparer.quote
    with _connection(bind) as connection:
        rows = connection.exec_driver_sql(
            "PRAGMA index_xinfo(" + quote(index["name"]) + ")"
        ).mappings()
        columns = [row for row in rows if row["key"] and row["cid"] >= 0]
    if [row["name"] for row in columns] != list(index["column_names"]):
        return None
    return tuple(bool(row["desc"]) for row in columns)

def _actual_index_signature(bind, index):
    columns = tuple(index["column_names"])
    if bind.dialect.name == "sqlite":
        descending = _sqlite_index_directions(bind, index)
    else:
        sorting = index.get("column_sorting") or {}
        descending = tuple("desc" in sorting.get(column, ()) for column in columns)
    return index["name"], columns, descending, bool(index["unique"])

def _expected_index_signature(index):
    columns = []
    descending = []
    for expression in index.expressions:
        descending.append(getattr(expression, "modifier", None) is sa.sql.operators.desc_op)
        columns.append(getattr(getattr(expression, "element", expression), "name"))
    return index.name, tuple(columns), tuple(descending), bool(index.unique)

def _sqlite_table_uses_autoincrement(bind, table_name):
    with _connection(bind) as connection:
        statement = sa.text(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :table_name"
        )
        sql = connection.scalar(statement, {"table_name": table_name}) or ""
    return bool(re.search(r"\\bAUTOINCREMENT\\b", sql, re.IGNORECASE))

def check_schema(bind):
    """Read-only adoption preflight. Return differences; never stamp or run DDL."""
    if bind.dialect.name != EXPECTED_DIALECT:
        return ["database dialect differs"]
    inspector = sa.inspect(bind)
    differences = []
    actual_tables = set(inspector.get_table_names())
    expected_tables = set(metadata.tables)
    for name in sorted(actual_tables - expected_tables - _ALLOWED_EXTRA_TABLES):
        differences.append(name + ": unexpected table")
    for name, table in sorted(metadata.tables.items()):
        if name not in actual_tables:
            differences.append(name + ": missing table")
            continue
        if bind.dialect.name == "sqlite":
            expected_autoincrement = any(
                column.primary_key and column.autoincrement is True for column in table.c
            )
            if _sqlite_table_uses_autoincrement(bind, name) != expected_autoincrement:
                differences.append(name + ": autoincrement differs")
        actual = {c["name"]: c for c in inspector.get_columns(name)}
        if set(actual) != set(table.c.keys()):
            differences.append(name + ": columns differ")
        for column in table.c:
            found = actual.get(column.name)
            if found is None:
                continue
            expected_type = column.type.compile(dialect=bind.dialect).upper()
            actual_type = found["type"].compile(dialect=bind.dialect).upper()
            if expected_type != actual_type:
                differences.append(name + "." + column.name + ": type differs")
            if found["nullable"] != column.nullable:
                differences.append(name + "." + column.name + ": nullability differs")
            default = found.get("default")
            if default is not None:
                if not _matches_autoincrement_default(bind, name, column, found):
                    differences.append(name + "." + column.name + ": server default differs")
            elif (bind.dialect.name == "postgresql" and column.primary_key
                  and column.autoincrement is True):
                differences.append(name + "." + column.name + ": autoincrement default missing")
        primary = tuple(inspector.get_pk_constraint(name)["constrained_columns"])
        if primary != tuple(c.name for c in table.primary_key):
            differences.append(name + ": primary key differs")
        actual_unique = [(item.get("name"), tuple(item["column_names"]))
                         for item in inspector.get_unique_constraints(name)]
        expected_unique = [(item.name, tuple(c.name for c in item.columns))
                           for item in table.constraints if isinstance(item, sa.UniqueConstraint)]
        if not _named_constraints_match(actual_unique, expected_unique):
            differences.append(name + ": unique constraints differ")
        actual_fk = []
        for item in inspector.get_foreign_keys(name):
            options = item.get("options") or {}
            actual_fk.append((item.get("name"), (
                tuple(item["constrained_columns"]), item.get("referred_schema"),
                item["referred_table"], tuple(item["referred_columns"]),
                options.get("ondelete"), options.get("onupdate"), options.get("deferrable"),
                options.get("initially"), options.get("match"),
            )))
        expected_fk = [(item.name, (
            tuple(element.parent.name for element in item.elements), item.referred_table.schema,
            item.referred_table.name, tuple(element.column.name for element in item.elements),
            item.ondelete, item.onupdate, item.deferrable, item.initially, item.match,
        )) for item in table.foreign_key_constraints]
        if not _named_constraints_match(actual_fk, expected_fk):
            differences.append(name + ": foreign keys differ")
        actual_indexes = {
            _actual_index_signature(bind, item)
            for item in inspector.get_indexes(name) if not item.get("duplicates_constraint")
        }
        expected_indexes = {_expected_index_signature(item) for item in table.indexes}
        if actual_indexes != expected_indexes:
            differences.append(name + ": indexes differ")
        normalize = lambda value: " ".join(str(value).replace('"', '').split())
        actual_checks = [(item.get("name"), normalize(item["sqltext"]))
                         for item in inspector.get_check_constraints(name)]
        expected_checks = [(item.name, normalize(item.sqltext)) for item in table.constraints
                           if isinstance(item, sa.CheckConstraint)]
        if not _named_constraints_match(actual_checks, expected_checks):
            differences.append(name + ": check constraints differ")
    return differences

def adopt_existing(engine, reviewed_schema_hash):
    """Explicitly stamp an exact existing schema; generation never calls this."""
    if reviewed_schema_hash != EXPECTED_SCHEMA_HASH:
        raise ValueError("reviewed schema hash does not match generated schema")
    version = sa.Table(
        "alembic_version", sa.MetaData(),
        sa.Column("version_num", sa.String(32), primary_key=True, nullable=False),
    )
    with engine.begin() as connection:
        differences = check_schema(connection)
        if differences:
            raise RuntimeError("schema adoption refused: " + "; ".join(differences))
        if "alembic_version" in sa.inspect(connection).get_table_names():
            revisions = list(connection.scalars(sa.select(version.c.version_num)))
            if revisions == [REVISION]:
                return REVISION
            raise RuntimeError("schema adoption refused: Alembic revision differs")
        version.create(connection)
        connection.execute(version.insert().values(version_num=REVISION))
    return REVISION
'''

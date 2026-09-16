# SPDX-License-Identifier: Apache-2.0
"""Capture database model facts independently of API serializer projections.

No database connection, source default callable or migration is executed here.
Unsupported constructs remain blocking gaps instead of disappearing from a schema.
"""

from __future__ import annotations

import datetime
import hashlib
import importlib
import json
import uuid
from decimal import Decimal
from typing import Any


def unsafe_table_name(name: str) -> bool:
    return "." in name or any(quote in name for quote in ('"', "'", "`", "[", "]"))


def order_tables(tables: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """Return stable dependency order and any tables blocked by an FK cycle."""
    by_name = {table["name"]: table for table in tables}
    if len(by_name) != len(tables):
        return sorted(tables, key=lambda table: (table["name"], table["model"])), tuple(
            sorted(table["name"] for table in tables)
        )
    dependencies = {
        name: {
            reference["table"]
            for column in table["columns"]
            if (reference := column.get("references"))
            and reference["table"] in by_name
            and reference["table"] != name
        }
        for name, table in by_name.items()
    }
    ordered: list[dict[str, Any]] = []
    emitted: set[str] = set()
    while len(emitted) < len(by_name):
        ready = sorted(
            name
            for name, required in dependencies.items()
            if name not in emitted and required <= emitted
        )
        if not ready:
            blocked = tuple(sorted(set(by_name) - emitted))
            ordered.extend(by_name[name] for name in blocked)
            return ordered, blocked
        ordered.extend(by_name[name] for name in ready)
        emitted.update(ready)
    return ordered, ()


def capture_schema(model_classes: list[Any] | None = None) -> dict[str, Any]:
    apps = importlib.import_module("django.apps").apps
    settings = importlib.import_module("django.conf").settings
    django_db = importlib.import_module("django.db")
    connections, models = django_db.connections, django_db.models
    NOT_PROVIDED = importlib.import_module("django.db.models.fields").NOT_PROVIDED
    timezone = importlib.import_module("django.utils.timezone")

    database = settings.DATABASES.get("default", {})
    engine = str(database.get("ENGINE", ""))
    dialect = (
        "sqlite"
        if engine == "django.db.backends.sqlite3"
        else ("postgresql" if engine == "django.db.backends.postgresql" else "unsupported")
    )
    gaps: list[dict[str, str]] = []
    schema_editor = connections["default"].schema_editor()

    def gap(owner: str, feature: str) -> None:
        gaps.append({"source": owner, "feature": feature})

    if dialect == "unsupported":
        gap("DATABASES.default", "database-dialect")
    if settings.DATABASE_ROUTERS or set(settings.DATABASES) != {"default"}:
        gap("DATABASES", "multiple-databases-or-router")
    kinds = {
        models.AutoField: "integer",
        models.BigAutoField: "big_integer",
        models.SmallAutoField: "small_integer",
        models.IntegerField: "integer",
        models.BigIntegerField: "big_integer",
        models.SmallIntegerField: "small_integer",
        models.PositiveIntegerField: "integer",
        models.PositiveBigIntegerField: "big_integer",
        models.PositiveSmallIntegerField: "small_integer",
        models.CharField: "string",
        models.TextField: "text",
        models.EmailField: "string",
        models.URLField: "string",
        models.SlugField: "string",
        models.BooleanField: "boolean",
        models.UUIDField: "uuid",
        models.DecimalField: "decimal",
        models.FloatField: "float",
        models.DateField: "date",
        models.DateTimeField: "datetime",
        models.TimeField: "time",
        models.BinaryField: "binary",
    }
    relations = {models.ForeignKey, models.OneToOneField}
    autos = {models.AutoField, models.BigAutoField, models.SmallAutoField}
    positive = {
        models.PositiveIntegerField,
        models.PositiveBigIntegerField,
        models.PositiveSmallIntegerField,
    }
    supported_delete = {
        models.CASCADE,
        models.PROTECT,
        models.RESTRICT,
        models.SET_NULL,
        models.DO_NOTHING,
    }
    pending = list(model_classes if model_classes is not None else apps.get_models())
    seen: dict[str, Any] = {}
    tables = []
    while pending:
        model = pending.pop()
        meta = model._meta
        if meta.proxy:
            pending.append(meta.concrete_model)
            continue
        if meta.label in seen:
            continue
        seen[meta.label] = model
        if meta.parents:
            gap(meta.label, "model-inheritance")
        if not meta.managed:
            gap(meta.label, "unmanaged-model")
        if unsafe_table_name(meta.db_table):
            gap(meta.label, "schema-qualified-or-quoted-table")
        for relation in meta.local_many_to_many:
            if type(relation) is not models.ManyToManyField:
                gap(meta.label + "." + relation.name, "custom-field")
            pending.extend([relation.remote_field.model, relation.remote_field.through])
        columns = []
        implicit_indexes = []
        for field in meta.local_fields:
            owner = meta.label + "." + field.name
            reference = None
            actual = field
            if type(field) in relations:
                target = field.target_field
                pending.append(field.remote_field.model)
                deletion = field.remote_field.on_delete
                if deletion not in supported_delete:
                    gap(owner, "on-delete")
                if not field.db_constraint:
                    gap(owner, "unconstrained-foreign-key")
                reference = {
                    "table": target.model._meta.db_table,
                    "column": target.column,
                    "on_delete": deletion.__name__,
                    "deferrable": True,
                    "initially": "DEFERRED",
                }
                actual = target
            elif type(field) not in kinds:
                gap(owner, "custom-field")
                continue
            if type(actual) not in kinds:
                gap(owner, "foreign-key-type")
                continue
            if getattr(field, "db_collation", None):
                gap(owner, "collation")
            if getattr(field, "db_default", NOT_PROVIDED) is not NOT_PROVIDED:
                gap(owner, "database-default")
            if getattr(field, "generated", False):
                gap(owner, "generated-column")
            default: dict[str, Any] | None = None
            if field.has_default():
                value = field.default
                if value is uuid.uuid4:
                    default = {"factory": "uuid4"}
                elif value is timezone.now:
                    default = {"factory": "now"}
                elif callable(value):
                    gap(owner, "callable-default")
                elif value is None or type(value) in (bool, int, float, str):
                    default = {"value": value}
                elif isinstance(value, Decimal):
                    default = {"decimal": str(value)}
                elif isinstance(value, uuid.UUID):
                    default = {"uuid": str(value)}
                elif isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
                    default = {"temporal": value.isoformat()}
                else:
                    gap(owner, "field-default")
            identity = None
            if type(field) in autos and dialect == "postgresql":
                suffix = field.db_type_suffix(connection=connections["default"])
                if suffix == "GENERATED BY DEFAULT AS IDENTITY":
                    identity = {"always": False}
                elif suffix:
                    gap(owner, "autoincrement-suffix")
            columns.append(
                {
                    "name": field.column,
                    "attribute": field.attname,
                    "kind": kinds[type(actual)],
                    "primary_key": field.primary_key,
                    "nullable": field.null,
                    "unique": field.unique,
                    "indexed": field.db_index,
                    "index_name": (
                        schema_editor._create_index_name(meta.db_table, [field.column], suffix="")
                        if field.db_index and not field.unique
                        else None
                    ),
                    "autoincrement": type(field) in autos,
                    "identity": identity,
                    "length": getattr(actual, "max_length", None),
                    "precision": getattr(actual, "max_digits", None),
                    "scale": getattr(actual, "decimal_places", None),
                    "positive": type(actual) in positive,
                    "default": default,
                    "auto_now": bool(getattr(field, "auto_now", False)),
                    "auto_now_add": bool(getattr(field, "auto_now_add", False)),
                    "references": reference,
                }
            )
            if (
                dialect == "postgresql"
                and type(actual) in {models.CharField, models.TextField}
                and (field.db_index or field.unique)
            ):
                implicit_indexes.append(
                    {
                        "name": schema_editor._create_index_name(
                            meta.db_table, [field.column], suffix="_like"
                        ),
                        "columns": [field.column],
                        "descending": [False],
                        "opclasses": [
                            "varchar_pattern_ops"
                            if type(actual) is models.CharField
                            else "text_pattern_ops"
                        ],
                    }
                )
        unique_constraints = []
        for fields in meta.unique_together:
            columns_for_constraint = [meta.get_field(field).column for field in fields]
            unique_constraints.append(
                {
                    "name": schema_editor._create_index_name(
                        meta.db_table, columns_for_constraint, suffix="_uniq"
                    ),
                    "columns": columns_for_constraint,
                }
            )
        for constraint in meta.constraints:
            if type(constraint) is not models.UniqueConstraint:
                gap(
                    meta.label + "." + constraint.name,
                    "check-constraint"
                    if isinstance(constraint, models.CheckConstraint)
                    else "constraint",
                )
            elif (
                not constraint.fields
                or constraint.condition
                or constraint.deferrable
                or constraint.include
                or constraint.opclasses
                or constraint.nulls_distinct is not None
            ):
                gap(meta.label + "." + constraint.name, "unique-constraint")
            else:
                unique_constraints.append(
                    {
                        "name": constraint.name,
                        "columns": [meta.get_field(f).column for f in constraint.fields],
                    }
                )
        indexes = list(implicit_indexes)
        for index in meta.indexes:
            if (
                type(index) is not models.Index
                or not index.fields
                or index.condition
                or index.include
                or index.opclasses
                or index.db_tablespace
            ):
                gap(meta.label + "." + index.name, "index")
            else:
                indexes.append(
                    {
                        "name": index.name,
                        "columns": [meta.get_field(f.lstrip("-")).column for f in index.fields],
                        "descending": [f.startswith("-") for f in index.fields],
                    }
                )
        tables.append(
            {
                "name": meta.db_table,
                "model": meta.label,
                "columns": sorted(columns, key=lambda c: c["name"]),
                "unique_constraints": sorted(unique_constraints, key=lambda c: c["name"]),
                "indexes": sorted(indexes, key=lambda i: i["name"]),
            }
        )
    names = [table["name"] for table in tables]
    if len(names) != len(set(names)):
        gap("models", "duplicate-table")
    ordered_tables, cycle = order_tables(tables)
    if cycle:
        gap("models", "foreign-key-cycle")
    result = {
        "schema_version": 1,
        "dialect": dialect,
        "use_tz": bool(settings.USE_TZ),
        "timezone": str(settings.TIME_ZONE),
        "connection_timezone": str(
            settings.TIME_ZONE if not settings.USE_TZ else database.get("TIME_ZONE") or "UTC"
        ),
        "tables": ordered_tables,
        "gaps": sorted(gaps, key=lambda g: (g["source"], g["feature"])),
    }
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {**result, "schema_hash": "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()}

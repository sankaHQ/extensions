# SPDX-License-Identifier: Apache-2.0
"""Static flat-model schema capture. Unknown ORM behavior blocks generation."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
DJANGO_TYPES = {
    "AutoField": ("integer", "int32"),
    "BigAutoField": ("bigint", "int64"),
    "IntegerField": ("integer", "int32"),
    "BigIntegerField": ("bigint", "int64"),
    "BooleanField": ("boolean", "bool"),
    "TextField": ("text", "string"),
    "CharField": ("varchar", "string"),
    "UUIDField": ("uuid", "UUIDValue"),
    "DateField": ("date", "DateValue"),
    "DateTimeField": ("timestamp with time zone", "TimestampValue"),
    "DecimalField": ("numeric", "DecimalValue"),
    "JSONField": ("jsonb", "JSONValue"),
}
SQLA_TYPES = {
    "Integer": ("integer", "int32"),
    "BigInteger": ("bigint", "int64"),
    "Boolean": ("boolean", "bool"),
    "Text": ("text", "string"),
    "String": ("varchar", "string"),
    "Uuid": ("uuid", "UUIDValue"),
    "Date": ("date", "DateValue"),
    "DateTime": ("timestamp with time zone", "TimestampValue"),
    "Numeric": ("numeric", "DecimalValue"),
    "JSONB": ("jsonb", "JSONValue"),
    "JSON": ("json", "JSONValue"),
}


def identifier(value: Any) -> str:
    if type(value) is not str or not IDENTIFIER.fullmatch(value):
        raise ValueError("schema identifiers must be lowercase ASCII and at most 63 characters")
    return value


def go_name(value: str) -> str:
    result = "".join(part.capitalize() for part in value.split("_"))
    if not result:
        raise ValueError("empty Go field name")
    return result


def _keywords(call: ast.Call, allowed: set[str]) -> dict[str, Any]:
    result = {}
    for item in call.keywords:
        if item.arg not in allowed or item.arg in result:
            raise ValueError("unsupported or duplicate model option: " + str(item.arg))
        result[item.arg] = ast.literal_eval(item.value)
    return result


def _field(
    name: str, sql_type: str, go_type: str, options: dict[str, Any], auto: bool
) -> dict[str, Any]:
    identifier(name)
    for key in ("primary_key", "nullable", "unique", "index", "none_as_null"):
        if key in options and type(options[key]) is not bool:
            raise ValueError(f"{key} must be boolean")
    primary = options.get("primary_key", False)
    nullable = options.get("nullable", False)
    if primary and (nullable or sql_type not in {"integer", "bigint", "uuid"}):
        raise ValueError("only non-null integer or UUID primary keys are qualified")
    if auto and (not primary or sql_type not in {"integer", "bigint"}):
        raise ValueError("auto increment requires the primary key")
    if options.get("index") and options.get("unique"):
        raise ValueError("combined index/unique ORM behavior requires separate capture")
    if sql_type != "numeric" and {"precision", "scale"} & options.keys():
        raise ValueError("precision and scale are only valid on decimal fields")
    length = options.get("length")
    if sql_type == "varchar":
        if type(length) is not int or not 0 < length <= 10485760:
            raise ValueError("varchar requires a positive bounded length")
        sql_type = f"varchar({length})"
    elif length is not None:
        raise ValueError("length is only supported for varchar")
    if sql_type == "numeric":
        precision, scale = options.get("precision"), options.get("scale")
        if (
            type(precision) is not int
            or type(scale) is not int
            or not 0 <= scale <= precision <= 1000
            or not precision
        ):
            raise ValueError("numeric requires explicit bounded precision and scale")
        sql_type = f"numeric({precision},{scale})"
    result = {
        "name": name,
        "sql_type": sql_type,
        "go_type": go_type,
        "nullable": nullable,
        "primary_key": primary,
        "unique": options.get("unique", False),
        "auto": auto,
    }
    if sql_type in {"json", "jsonb"}:
        result["none_as_null"] = options.get("none_as_null", True)
    for key in ("default", "index"):
        if key in options:
            value = options[key]
            if key == "default":
                expected = {"string": str, "bool": bool, "int32": int, "int64": int}
                if go_type not in expected or type(value) is not expected[go_type] or primary:
                    raise ValueError("only static scalar non-primary defaults are qualified")
                if go_type in {"int32", "int64"}:
                    bits = 32 if go_type == "int32" else 64
                    if not -(2 ** (bits - 1)) <= value < 2 ** (bits - 1):
                        raise ValueError("default exceeds integer bounds")
                if length is not None and isinstance(value, str) and len(value) > length:
                    raise ValueError("default exceeds column length")
            result[key] = value
    return result


def _table_metadata(
    value: ast.expr, django: bool, unique: bool | None = None
) -> list[dict[str, Any]]:
    if not isinstance(value, (ast.List, ast.Tuple)):
        raise ValueError("table constraints require a static list or tuple")
    result = []
    for call in value.elts:
        if not isinstance(call, ast.Call):
            raise ValueError("table constraints require explicit declarations")
        kind = ast.unparse(call.func)
        prefix = "models." if django else ""
        if kind not in {prefix + "Index", prefix + "UniqueConstraint"}:
            raise ValueError("unsupported table constraint")
        is_unique = kind.endswith("UniqueConstraint")
        if unique is not None and unique != is_unique:
            raise ValueError("constraint is in the wrong metadata list")
        options = _keywords(call, {"fields", "name"} if django else {"name"})
        if django:
            if call.args or "fields" not in options:
                raise ValueError("Django indexes require explicit fields")
            columns = options["fields"]
        else:
            args = [ast.literal_eval(arg) for arg in call.args]
            if not is_unique:
                if not args or "name" in options:
                    raise ValueError("Index requires one explicit name")
                options["name"], args = args[0], args[1:]
            columns = args
        if (
            not isinstance(columns, (list, tuple))
            or not columns
            or len(set(columns)) != len(columns)
        ):
            raise ValueError("constraint columns must be distinct explicit fields")
        entry: dict[str, Any] = {
            "name": identifier(options.get("name")),
            "columns": [identifier(column) for column in columns],
        }
        if is_unique:
            entry["unique"] = True
        result.append(entry)
    return sorted(result, key=lambda item: item["name"])


def _django(model: ast.ClassDef) -> dict[str, Any]:
    if [ast.unparse(base) for base in model.bases] != ["models.Model"]:
        raise ValueError("only direct Django model classes are qualified")
    fields = []
    metadata: dict[str, Any] = {}
    for node in model.body:
        if isinstance(node, ast.ClassDef) and node.name == "Meta":
            if metadata or node.bases or node.keywords or node.decorator_list or node.type_params:
                raise ValueError("unsupported model Meta")
            for setting in node.body:
                if not (
                    isinstance(setting, ast.Assign)
                    and len(setting.targets) == 1
                    and isinstance(setting.targets[0], ast.Name)
                ):
                    raise ValueError("model Meta requires literal settings")
                key = setting.targets[0].id
                if (
                    key not in {"app_label", "db_table", "constraints", "indexes"}
                    or key in metadata
                ):
                    raise ValueError("unsupported model Meta setting: " + key)
                metadata[key] = (
                    _table_metadata(setting.value, True, key == "constraints")
                    if key in {"constraints", "indexes"}
                    else identifier(ast.literal_eval(setting.value))
                )
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            field_name = node.targets[0].id
            if field_name in {"pk", "objects"} or "__" in field_name or field_name.endswith("_"):
                raise ValueError("Django field name conflicts with ORM lookup or manager semantics")
            call = node.value
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "models"
                and call.func.attr in {*DJANGO_TYPES, "ForeignKey"}
            ):
                raise ValueError("unsupported Django field or model attribute")
            if call.func.attr == "ForeignKey":
                if len(call.args) != 1 or not isinstance(call.args[0], ast.Name):
                    raise ValueError("ForeignKey requires an earlier explicit model")
                deletion = [item for item in call.keywords if item.arg == "on_delete"]
                if len(deletion) != 1 or ast.unparse(deletion[0].value) != "models.DO_NOTHING":
                    raise ValueError("Django Python deletion policies require separate capture")
                options = _keywords(
                    ast.Call(
                        func=call.func,
                        args=[],
                        keywords=[item for item in call.keywords if item.arg != "on_delete"],
                    ),
                    {"null", "db_index"},
                )
                if type(options.get("db_index", True)) is not bool:
                    raise ValueError("db_index must be boolean")
                field = _field(
                    field_name + "_id",
                    "bigint",
                    "int64",
                    {
                        "nullable": options.get("null", False),
                    },
                    False,
                )
                field["reference_model"] = call.args[0].id
                field["index"] = options.get("db_index", True)
                fields.append(field)
                continue
            if call.args:
                raise ValueError("positional Django field arguments require capture")
            options = _keywords(
                call,
                {
                    "primary_key",
                    "null",
                    "unique",
                    "max_length",
                    "max_digits",
                    "decimal_places",
                    "default",
                    "db_index",
                },
            )
            options = {
                {
                    "null": "nullable",
                    "max_length": "length",
                    "max_digits": "precision",
                    "decimal_places": "scale",
                    "db_index": "index",
                }.get(key, key): value
                for key, value in options.items()
            }
            sql_type, go_type = DJANGO_TYPES[call.func.attr]
            fields.append(
                _field(
                    node.targets[0].id,
                    sql_type,
                    go_type,
                    options,
                    call.func.attr in {"AutoField", "BigAutoField"},
                )
            )
        else:
            raise ValueError("model methods, validation and custom metadata require capture")
    if not {"app_label", "db_table"} <= set(metadata):
        raise ValueError("Django models require explicit app_label and db_table")
    return {
        "name": model.name,
        "table": metadata["db_table"],
        "fields": fields,
        **{key: metadata[key] for key in ("constraints", "indexes") if key in metadata},
    }


def _sqlalchemy(model: ast.ClassDef) -> dict[str, Any]:
    if [ast.unparse(base) for base in model.bases] != ["Base"]:
        raise ValueError("only direct SQLAlchemy Base subclasses are qualified")
    table = None
    fields = []
    metadata: dict[str, Any] = {}
    for node in model.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "__tablename__"
        ):
            if table is not None:
                raise ValueError("duplicate table name declaration")
            table = identifier(ast.literal_eval(node.value))
            continue
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "__table_args__"
        ):
            if metadata:
                raise ValueError("duplicate table constraints")
            entries = _table_metadata(node.value, False)
            metadata = {
                "constraints": [item for item in entries if item.get("unique")],
                "indexes": [item for item in entries if not item.get("unique")],
            }
            continue
        if not (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.annotation, ast.Subscript)
            and isinstance(node.annotation.value, ast.Name)
            and node.annotation.value.id == "Mapped"
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "mapped_column"
        ):
            raise ValueError("only explicit Mapped columns are qualified")
        annotation = ast.unparse(node.annotation.slice)
        nullable = annotation.endswith(" | None")
        annotation = annotation.removesuffix(" | None")
        if annotation not in {"int", "str", "bool", "UUID", "date", "datetime", "Decimal", "dict"}:
            raise ValueError("unsupported column annotation")
        call = node.value
        options = _keywords(
            call, {"primary_key", "nullable", "unique", "autoincrement", "default", "index"}
        )
        options.setdefault("nullable", nullable)
        if options["nullable"] and "default" in options:
            raise ValueError("nullable SQLAlchemy defaults require operation-specific capture")
        inferred = {
            "int": "Integer",
            "str": "String",
            "bool": "Boolean",
            "UUID": "Uuid",
            "date": "Date",
            "datetime": "DateTime",
            "Decimal": "Numeric",
            "dict": "JSONB",
        }[annotation]
        type_name = inferred
        if annotation not in {"int", "str", "bool"} and not call.args:
            raise ValueError("rich columns require an explicit SQL type")
        reference = None
        if len(call.args) == 2:
            foreign = call.args[1]
            if not (
                isinstance(foreign, ast.Call)
                and isinstance(foreign.func, ast.Name)
                and foreign.func.id == "ForeignKey"
                and len(foreign.args) == 1
            ):
                raise ValueError("unsupported column constraint")
            target = ast.literal_eval(foreign.args[0])
            if type(target) is not str or len(target.split(".")) != 2:
                raise ValueError("ForeignKey requires a captured table.column")
            table_name, column = map(identifier, target.split("."))
            opts = _keywords(foreign, {"ondelete", "deferrable", "initially"})
            action = opts.get("ondelete", "NO ACTION")
            deferrable = opts.get("deferrable", False)
            initially = opts.get("initially", "IMMEDIATE")
            if (
                action not in {"NO ACTION", "RESTRICT", "CASCADE", "SET NULL"}
                or type(deferrable) is not bool
                or initially not in {"IMMEDIATE", "DEFERRED"}
                or (initially == "DEFERRED" and not deferrable)
                or (action == "SET NULL" and not options["nullable"])
            ):
                raise ValueError("unsupported foreign-key behavior")
            reference = {
                "table": table_name,
                "column": column,
                "on_delete": action,
                "deferrable": deferrable,
                "deferred": initially == "DEFERRED",
            }
        elif len(call.args) > 2:
            raise ValueError("multiple column constraints require capture")
        if call.args:
            datatype = call.args[0]
            if isinstance(datatype, ast.Call) and isinstance(datatype.func, ast.Name):
                type_name = datatype.func.id
                if type_name == "String" and len(datatype.args) == 1 and not datatype.keywords:
                    options["length"] = ast.literal_eval(datatype.args[0])
                elif type_name == "Numeric" and len(datatype.args) == 2:
                    opts = _keywords(datatype, {"asdecimal"})
                    if opts.get("asdecimal", True) is not True:
                        raise ValueError("numeric must preserve exact decimals")
                    options.update(
                        precision=ast.literal_eval(datatype.args[0]),
                        scale=ast.literal_eval(datatype.args[1]),
                    )
                elif type_name == "DateTime" and not datatype.args:
                    if _keywords(datatype, {"timezone"}) != {"timezone": True}:
                        raise ValueError("timestamps require explicit timezone=True")
                elif type_name in {"JSON", "JSONB"} and not datatype.args:
                    options.update(_keywords(datatype, {"none_as_null"}))
                    options.setdefault("none_as_null", False)
                else:
                    raise ValueError("unsupported SQLAlchemy type arguments")
            elif isinstance(datatype, ast.Name):
                type_name = datatype.id
                if type_name == "DateTime":
                    raise ValueError("timestamps require explicit timezone=True")
                if type_name in {"JSON", "JSONB"}:
                    options["none_as_null"] = False
            else:
                raise ValueError("unsupported SQLAlchemy type")
        if type_name not in SQLA_TYPES:
            raise ValueError("unsupported SQLAlchemy type: " + type_name)
        sql_type, go_type = SQLA_TYPES[type_name]
        expected = {
            "int32": "int",
            "int64": "int",
            "string": "str",
            "bool": "bool",
            "UUIDValue": "UUID",
            "DateValue": "date",
            "TimestampValue": "datetime",
            "DecimalValue": "Decimal",
            "JSONValue": "dict",
        }[go_type]
        if annotation != expected:
            raise ValueError("column annotation differs from SQL type")
        auto = options.pop(
            "autoincrement", options.get("primary_key", False) and sql_type in {"integer", "bigint"}
        )
        if type(auto) is not bool:
            raise ValueError("autoincrement must be boolean")
        field = _field(node.target.id, sql_type, go_type, options, auto)
        if reference:
            if field["primary_key"]:
                raise ValueError("shared primary-key relationships require capture")
            field["references"] = reference
        fields.append(field)
    if table is None:
        raise ValueError("SQLAlchemy models require __tablename__")
    return {"name": model.name, "table": table, "fields": fields, **metadata}


def capture_models(path: Path, framework: str) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("models_file must be a regular file")
    allowed = (
        {"django.db": {"models"}}
        if framework == "drf"
        else {
            "sqlalchemy.orm": {"DeclarativeBase", "Mapped", "mapped_column"},
            "sqlalchemy": {
                *(set(SQLA_TYPES) - {"JSONB"}),
                "ForeignKey",
                "UniqueConstraint",
                "Index",
            },
            "sqlalchemy.dialects.postgresql": {"JSONB"},
            "uuid": {"UUID"},
            "datetime": {"date", "datetime"},
            "decimal": {"Decimal"},
        }
    )
    models = []
    names: set[str] = set()
    base_seen = False
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.ImportFrom) and not node.level and node.module in allowed:
            for alias in node.names:
                if alias.asname or alias.name not in allowed[node.module] or alias.name in names:
                    raise ValueError("unsupported model import")
                names.add(alias.name)
        elif isinstance(node, ast.ClassDef):
            if node.keywords or node.decorator_list or node.type_params or node.name in names:
                raise ValueError("custom model class behavior or duplicate name")
            if framework != "drf" and node.name == "Base":
                if (
                    [ast.unparse(base) for base in node.bases] != ["DeclarativeBase"]
                    or "DeclarativeBase" not in names
                    or len(node.body) != 1
                    or not isinstance(node.body[0], ast.Pass)
                ):
                    raise ValueError("Base must be an unmodified DeclarativeBase subclass")
                base_seen = True
            else:
                if framework != "drf" and not base_seen:
                    raise ValueError("declare Base before models")
                referenced = {
                    item.id
                    for item in ast.walk(node)
                    if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
                }
                if referenced - names - {"int", "str", "bool", "dict"}:
                    raise ValueError("model references an unresolved symbol")
                if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", node.name) or node.name in {
                    "NewApp",
                    "Migrate",
                    "UUIDValue",
                    "DateValue",
                    "TimestampValue",
                    "DecimalValue",
                    "JSONValue",
                }:
                    raise ValueError("model names must be exported Go identifiers")
                models.append(_django(node) if framework == "drf" else _sqlalchemy(node))
            names.add(node.name)
        else:
            raise ValueError("unsupported statement in models file")
    if not models:
        raise ValueError("no qualified models")
    if len({model["table"] for model in models}) != len(models):
        raise ValueError("duplicate database table")
    for model in models:
        if model["table"] in {"goose_db_version", "goose_db_version_id_seq"}:
            raise ValueError("model table conflicts with migration bookkeeping")
        fields = model["fields"]
        if sum(field["primary_key"] for field in fields) != 1:
            raise ValueError("models require one explicit primary key")
        entries = [*model.get("constraints", []), *model.get("indexes", [])]
        if len({entry["name"] for entry in entries}) != len(entries):
            raise ValueError("duplicate constraint or index name")
        if any(set(entry["columns"]) - {field["name"] for field in fields} for entry in entries):
            raise ValueError("constraint references an uncaptured column")
        if len({go_name(field["name"]) for field in fields}) != len(fields):
            raise ValueError("duplicate or colliding field names")
    by_name = {model["name"]: model for model in models}
    by_table = {model["table"]: model for model in models}
    index_names = [
        entry["name"]
        for model in models
        for entry in [*model.get("constraints", []), *model.get("indexes", [])]
    ]
    if len(set(index_names)) != len(index_names) or set(index_names) & set(by_table):
        raise ValueError("index names must be unique across the schema")
    dependencies: dict[str, set[str]] = {table: set() for table in by_table}
    for model in models:
        for field in model["fields"]:
            if "reference_model" in field:
                parent = by_name.get(field.pop("reference_model"))
                if parent is None:
                    raise ValueError("foreign key references an uncaptured model")
                primary = next(item for item in parent["fields"] if item["primary_key"])
                field.update(sql_type=primary["sql_type"], go_type=primary["go_type"])
                field["references"] = {
                    "table": parent["table"],
                    "column": primary["name"],
                    "on_delete": "NO ACTION",
                    "deferrable": True,
                    "deferred": True,
                }
            if "references" not in field:
                continue
            reference = field["references"]
            parent = by_table.get(reference["table"])
            if parent is None:
                raise ValueError("foreign key references an uncaptured table")
            primary = next(item for item in parent["fields"] if item["primary_key"])
            if primary["name"] != reference["column"] or primary["sql_type"] != field["sql_type"]:
                raise ValueError("foreign key must match the captured primary-key type")
            dependencies[model["table"]].add(parent["table"])
    ordered = []
    while dependencies:
        ready = sorted(table for table, parents in dependencies.items() if not parents)
        if not ready:
            raise ValueError("cyclic foreign keys require separate migration capture")
        for table in ready:
            ordered.append(by_table[table])
            del dependencies[table]
        for parents in dependencies.values():
            parents.difference_update(ready)
    return ordered

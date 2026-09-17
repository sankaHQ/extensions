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
}
SQLA_TYPES = {
    "Integer": ("integer", "int32"),
    "BigInteger": ("bigint", "int64"),
    "Boolean": ("boolean", "bool"),
    "Text": ("text", "string"),
    "String": ("varchar", "string"),
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
    for key in ("primary_key", "nullable", "unique"):
        if key in options and type(options[key]) is not bool:
            raise ValueError(f"{key} must be boolean")
    primary = options.get("primary_key", False)
    nullable = options.get("nullable", False)
    if primary and (nullable or sql_type not in {"integer", "bigint"}):
        raise ValueError("only non-null integer primary keys are qualified")
    if auto and not primary:
        raise ValueError("auto increment requires the primary key")
    length = options.get("length")
    if sql_type == "varchar":
        if type(length) is not int or not 0 < length <= 10485760:
            raise ValueError("varchar requires a positive bounded length")
        sql_type = f"varchar({length})"
    elif length is not None:
        raise ValueError("length is only supported for varchar")
    return {
        "name": name,
        "sql_type": sql_type,
        "go_type": go_type,
        "nullable": nullable,
        "primary_key": primary,
        "unique": options.get("unique", False),
        "auto": auto,
    }


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
                if key not in {"app_label", "db_table"} or key in metadata:
                    raise ValueError("unsupported model Meta setting: " + key)
                metadata[key] = identifier(ast.literal_eval(setting.value))
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            call = node.value
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "models"
                and call.func.attr in DJANGO_TYPES
                and not call.args
            ):
                raise ValueError("unsupported Django field or model attribute")
            options = _keywords(call, {"primary_key", "null", "unique", "max_length"})
            options = {
                {"null": "nullable", "max_length": "length"}.get(key, key): value
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
    if set(metadata) != {"app_label", "db_table"}:
        raise ValueError("Django models require explicit app_label and db_table")
    return {"name": model.name, "table": metadata["db_table"], "fields": fields}


def _sqlalchemy(model: ast.ClassDef) -> dict[str, Any]:
    if [ast.unparse(base) for base in model.bases] != ["Base"]:
        raise ValueError("only direct SQLAlchemy Base subclasses are qualified")
    table = None
    fields = []
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
        if annotation not in {"int", "str", "bool"}:
            raise ValueError("unsupported column annotation")
        call = node.value
        options = _keywords(call, {"primary_key", "nullable", "unique", "autoincrement"})
        options.setdefault("nullable", nullable)
        inferred = {"int": "Integer", "str": "String", "bool": "Boolean"}[annotation]
        type_name = inferred
        if call.args:
            if len(call.args) != 1:
                raise ValueError("relationships and multiple column arguments require capture")
            datatype = call.args[0]
            if isinstance(datatype, ast.Call) and isinstance(datatype.func, ast.Name):
                type_name = datatype.func.id
                if type_name != "String" or len(datatype.args) != 1 or datatype.keywords:
                    raise ValueError("only String(length) type arguments are qualified")
                options["length"] = ast.literal_eval(datatype.args[0])
            elif isinstance(datatype, ast.Name):
                type_name = datatype.id
            else:
                raise ValueError("unsupported SQLAlchemy type")
        if type_name not in SQLA_TYPES:
            raise ValueError("unsupported SQLAlchemy type: " + type_name)
        sql_type, go_type = SQLA_TYPES[type_name]
        expected = {"int32": "int", "int64": "int", "string": "str", "bool": "bool"}[go_type]
        if annotation != expected:
            raise ValueError("column annotation differs from SQL type")
        auto = options.pop("autoincrement", options.get("primary_key", False))
        if type(auto) is not bool:
            raise ValueError("autoincrement must be boolean")
        fields.append(_field(node.target.id, sql_type, go_type, options, auto))
    if table is None:
        raise ValueError("SQLAlchemy models require __tablename__")
    return {"name": model.name, "table": table, "fields": fields}


def capture_models(path: Path, framework: str) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("models_file must be a regular file")
    allowed = (
        {"django.db": {"models"}}
        if framework == "drf"
        else {
            "sqlalchemy.orm": {"DeclarativeBase", "Mapped", "mapped_column"},
            "sqlalchemy": set(SQLA_TYPES),
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
                if referenced - names - {"int", "str", "bool"}:
                    raise ValueError("model references an unresolved symbol")
                if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", node.name) or node.name in {
                    "NewApp",
                    "Migrate",
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
        if model["table"] == "goose_db_version":
            raise ValueError("model table conflicts with migration bookkeeping")
        fields = model["fields"]
        if sum(field["primary_key"] for field in fields) != 1:
            raise ValueError("models require one explicit integer primary key")
        if len({go_name(field["name"]) for field in fields}) != len(fields):
            raise ValueError("duplicate or colliding field names")
    return sorted(models, key=lambda model: model["table"])

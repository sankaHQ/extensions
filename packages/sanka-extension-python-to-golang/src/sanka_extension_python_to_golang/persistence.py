# SPDX-License-Identifier: Apache-2.0
"""Static FastAPI validation and persistence capture. Source is never imported."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from .models import SQLA_TYPES, identifier

IGNORED = {".git", ".sanka", ".venv", "__pycache__"}
PYDANTIC_FIELD_OPTIONS = {
    "alias",
    "default",
    "default_factory",
    "description",
    "discriminator",
    "exclude",
    "examples",
    "ge",
    "gt",
    "le",
    "lt",
    "max_length",
    "max_digits",
    "decimal_places",
    "min_length",
    "multiple_of",
    "pattern",
    "serialization_alias",
    "strict",
    "validation_alias",
}
MIGRATION_OPERATIONS = {
    "add_column",
    "alter_column",
    "create_check_constraint",
    "create_foreign_key",
    "create_index",
    "create_primary_key",
    "create_table",
    "create_unique_constraint",
    "drop_column",
    "drop_constraint",
    "drop_index",
    "drop_table",
    "rename_table",
}
SESSION_OPERATIONS = {
    "add",
    "add_all",
    "commit",
    "delete",
    "execute",
    "flush",
    "get",
    "refresh",
    "rollback",
    "scalar",
    "scalars",
}


def _jsonable(value: Any) -> Any:
    if value is None or type(value) in {str, bool, int, float}:
        return value
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict) and all(type(key) is str for key in value):
        return {key: _jsonable(item) for key, item in sorted(value.items())}
    raise ValueError("value must be a literal JSON value")


def _json_literal(node: ast.expr) -> Any:
    return _jsonable(ast.literal_eval(node))


def _literal_keywords(call: ast.Call, *, exclude: frozenset[str] = frozenset()) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for keyword in call.keywords:
        if keyword.arg is None or keyword.arg in exclude or keyword.arg in result:
            raise ValueError("dynamic or duplicate keyword")
        result[keyword.arg] = _json_literal(keyword.value)
    return dict(sorted(result.items()))


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_call_name(node.value)}.{node.attr}"
    return ast.unparse(node)


def _nullable(node: ast.expr) -> bool:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _nullable(node.left) or _nullable(node.right)
    if isinstance(node, ast.Constant) and node.value is None:
        return True
    return isinstance(node, ast.Subscript) and _call_name(node.value) in {
        "Optional",
        "typing.Optional",
    }


def _field_value(node: ast.expr | None) -> tuple[bool, dict[str, Any]]:
    if node is None:
        return True, {}
    if not isinstance(node, ast.Call) or _call_name(node.func) != "Field":
        return False, {"default": _json_literal(node)}
    if len(node.args) > 1:
        raise ValueError("Field accepts at most one captured default")
    options: dict[str, Any] = {}
    if node.args and not (
        isinstance(node.args[0], ast.Constant) and node.args[0].value is Ellipsis
    ):
        options["default"] = _json_literal(node.args[0])
    for keyword in node.keywords:
        if (
            keyword.arg is None
            or keyword.arg not in PYDANTIC_FIELD_OPTIONS
            or keyword.arg in options
        ):
            raise ValueError("unsupported or duplicate Field option")
        if keyword.arg == "default_factory":
            if not isinstance(keyword.value, ast.Name):
                raise ValueError("default_factory must be a direct callable name")
            options[keyword.arg] = keyword.value.id
        elif keyword.arg == "default" and (
            isinstance(keyword.value, ast.Constant) and keyword.value.value is Ellipsis
        ):
            continue
        else:
            options[keyword.arg] = _json_literal(keyword.value)
    required = "default" not in options and "default_factory" not in options
    return required, dict(sorted(options.items()))


def _pydantic_models(path: Path, tree: ast.Module, gaps: list[str]) -> list[dict[str, Any]]:
    relative = path.as_posix()
    result = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or [_call_name(base) for base in node.bases] != [
            "BaseModel"
        ]:
            continue
        config: dict[str, Any] = {}
        fields = []
        validators = []
        for item in node.body:
            try:
                if (
                    isinstance(item, ast.Assign)
                    and len(item.targets) == 1
                    and isinstance(item.targets[0], ast.Name)
                    and item.targets[0].id == "model_config"
                    and isinstance(item.value, ast.Call)
                    and _call_name(item.value.func) == "ConfigDict"
                    and not item.value.args
                ):
                    config = _literal_keywords(item.value)
                elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    required, field = _field_value(item.value)
                    fields.append(
                        {
                            "name": item.target.id,
                            "annotation": ast.unparse(item.annotation),
                            "required": required,
                            "nullable": _nullable(item.annotation),
                            "field": field,
                        }
                    )
                elif isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    decorators = [
                        decorator
                        for decorator in item.decorator_list
                        if isinstance(decorator, ast.Call)
                        and _call_name(decorator.func) in {"field_validator", "model_validator"}
                    ]
                    if not decorators:
                        raise ValueError("model methods require behavioral capture")
                    for decorator in decorators:
                        validators.append(
                            {
                                "name": item.name,
                                "kind": _call_name(decorator.func),
                                "arguments": [ast.unparse(argument) for argument in decorator.args],
                                "options": _literal_keywords(decorator),
                            }
                        )
                    gaps.append(
                        f"{relative}:{node.name}.{item.name}: custom Pydantic validator "
                        "requires lowering"
                    )
                elif not isinstance(item, ast.Pass):
                    raise ValueError("unsupported Pydantic model member")
            except (TypeError, ValueError) as error:
                gaps.append(f"{relative}:{node.name}: {error}")
        result.append(
            {
                "module": relative,
                "name": node.name,
                "config": config,
                "fields": fields,
                "validators": validators,
            }
        )
    return result


def _foreign_key(call: ast.Call) -> dict[str, Any]:
    if len(call.args) != 1:
        raise ValueError("ForeignKey requires one literal target")
    target = _json_literal(call.args[0])
    if type(target) is not str:
        raise ValueError("ForeignKey target must be a string")
    return {"target": target, "options": _literal_keywords(call)}


def _sqlalchemy_models(path: Path, tree: ast.Module, gaps: list[str]) -> list[dict[str, Any]]:
    relative = path.as_posix()
    result = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or [_call_name(base) for base in node.bases] != [
            "Base"
        ]:
            continue
        table = None
        columns = []
        relationships = []
        table_args: str | None = None
        for item in node.body:
            try:
                if (
                    isinstance(item, ast.Assign)
                    and len(item.targets) == 1
                    and isinstance(item.targets[0], ast.Name)
                    and item.targets[0].id == "__tablename__"
                ):
                    table = _json_literal(item.value)
                elif (
                    isinstance(item, ast.Assign)
                    and len(item.targets) == 1
                    and isinstance(item.targets[0], ast.Name)
                    and item.targets[0].id == "__table_args__"
                ):
                    table_args = ast.unparse(item.value)
                elif (
                    isinstance(item, ast.AnnAssign)
                    and isinstance(item.target, ast.Name)
                    and isinstance(item.annotation, ast.Subscript)
                    and isinstance(item.value, ast.Call)
                    and _call_name(item.annotation.value) == "Mapped"
                ):
                    function = _call_name(item.value.func)
                    if function == "relationship":
                        if item.value.args:
                            raise ValueError("relationship target must come from Mapped annotation")
                        relationships.append(
                            {
                                "name": item.target.id,
                                "annotation": ast.unparse(item.annotation.slice),
                                "options": _literal_keywords(item.value),
                            }
                        )
                        continue
                    if function != "mapped_column":
                        raise ValueError("Mapped values must use mapped_column or relationship")
                    foreign_keys = []
                    type_expression = None
                    for argument in item.value.args:
                        if (
                            isinstance(argument, ast.Call)
                            and _call_name(argument.func) == "ForeignKey"
                        ):
                            foreign_keys.append(_foreign_key(argument))
                        elif type_expression is None:
                            type_expression = ast.unparse(argument)
                        else:
                            raise ValueError("multiple SQL column type expressions")
                    columns.append(
                        {
                            "name": item.target.id,
                            "annotation": ast.unparse(item.annotation.slice),
                            "type": type_expression,
                            "foreign_keys": foreign_keys,
                            "options": _literal_keywords(item.value),
                        }
                    )
                elif isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    raise ValueError("model methods require behavioral capture")
                elif not isinstance(item, ast.Pass):
                    raise ValueError("unsupported SQLAlchemy model member")
            except (TypeError, ValueError) as error:
                gaps.append(f"{relative}:{node.name}: {error}")
        if type(table) is not str:
            gaps.append(f"{relative}:{node.name}: literal __tablename__ is required")
            continue
        model = {
            "module": relative,
            "name": node.name,
            "table": table,
            "columns": columns,
            "relationships": relationships,
        }
        if table_args is not None:
            model["table_args"] = table_args
        result.append(model)
    return result


def _migration_operations(
    relative: str, function: ast.FunctionDef | ast.AsyncFunctionDef | None, gaps: list[str]
) -> list[dict[str, Any]]:
    if function is None:
        gaps.append(f"{relative}: upgrade and downgrade functions are required")
        return []
    if isinstance(function, ast.AsyncFunctionDef) or ast.unparse(function.args):
        gaps.append(
            f"{relative}:{function.name}: migration function must be synchronous and argument-free"
        )
        return []
    result = []
    for item in function.body:
        if not (
            isinstance(item, ast.Expr)
            and isinstance(item.value, ast.Call)
            and isinstance(item.value.func, ast.Attribute)
            and isinstance(item.value.func.value, ast.Name)
            and item.value.func.value.id == "op"
            and item.value.func.attr in MIGRATION_OPERATIONS
        ):
            gaps.append(f"{relative}:{function.name}: dynamic migration operation")
            continue
        try:
            options = {
                keyword.arg: ast.unparse(keyword.value)
                for keyword in item.value.keywords
                if keyword.arg is not None
            }
            if len(options) != len(item.value.keywords):
                raise ValueError("dynamic migration keyword")
            result.append(
                {
                    "name": item.value.func.attr,
                    "arguments": [ast.unparse(argument) for argument in item.value.args],
                    "options": dict(sorted(options.items())),
                }
            )
        except ValueError as error:
            gaps.append(f"{relative}:{function.name}: {error}")
    return result


def _migration(path: Path, tree: ast.Module, gaps: list[str]) -> dict[str, Any] | None:
    relative = path.as_posix()
    metadata: dict[str, Any] = {}
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and type(node.value.value) is str
        ):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "alembic" and not node.level:
            if len(node.names) != 1 or node.names[0].name != "op" or node.names[0].asname:
                gaps.append(f"{relative}: unsupported migration import")
            continue
        if isinstance(node, ast.Import):
            if (
                len(node.names) != 1
                or node.names[0].name != "sqlalchemy"
                or node.names[0].asname != "sa"
            ):
                gaps.append(f"{relative}: unsupported migration import")
            continue
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in {"revision", "down_revision", "branch_labels", "depends_on"}
        ):
            if node.targets[0].id in metadata:
                gaps.append(f"{relative}: duplicate revision metadata")
            try:
                metadata[node.targets[0].id] = _json_literal(node.value)
            except (TypeError, ValueError) as error:
                gaps.append(f"{relative}:{node.targets[0].id}: {error}")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in {
            "upgrade",
            "downgrade",
        }:
            if node.name in functions or node.decorator_list or node.returns:
                gaps.append(f"{relative}: unsupported migration function")
            functions[node.name] = node
        else:
            gaps.append(f"{relative}: unsupported top-level migration statement")
    if "revision" not in metadata:
        return None
    if type(metadata["revision"]) is not str or not metadata["revision"]:
        gaps.append(f"{relative}: revision must be a non-empty string")
    missing = {"down_revision", "branch_labels", "depends_on"} - metadata.keys()
    if missing:
        gaps.append(f"{relative}: missing revision metadata: {', '.join(sorted(missing))}")
    return {
        "module": relative,
        "revision": metadata.get("revision"),
        "down_revision": metadata.get("down_revision"),
        "branch_labels": metadata.get("branch_labels"),
        "depends_on": metadata.get("depends_on"),
        "upgrade": _migration_operations(relative, functions.get("upgrade"), gaps),
        "downgrade": _migration_operations(relative, functions.get("downgrade"), gaps),
    }


def _session_parameter(function: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    for argument in [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]:
        if argument.annotation is not None and _call_name(argument.annotation) == "AsyncSession":
            return argument.arg
    return None


def _receiver_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    ):
        return f"self.{node.attr}"
    return None


def _constructor_session(node: ast.ClassDef) -> str | None:
    constructor = next(
        (
            item
            for item in node.body
            if isinstance(item, ast.FunctionDef) and item.name == "__init__"
        ),
        None,
    )
    if constructor is None:
        return None
    parameter = _session_parameter(constructor)
    if parameter is None:
        return None
    assignments = [
        item
        for item in constructor.body
        if isinstance(item, ast.Assign)
        and len(item.targets) == 1
        and isinstance(item.targets[0], ast.Attribute)
        and isinstance(item.targets[0].value, ast.Name)
        and item.targets[0].value.id == "self"
        and isinstance(item.value, ast.Name)
        and item.value.id == parameter
    ]
    if len(assignments) != 1:
        return None
    target = assignments[0].targets[0]
    assert isinstance(target, ast.Attribute)
    return f"self.{target.attr}"


def _repositories(path: Path, tree: ast.Module, gaps: list[str]) -> list[dict[str, Any]]:
    relative = path.as_posix()
    result = []
    functions: list[tuple[str | None, str | None, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            functions.append((None, None, node))
        elif isinstance(node, ast.ClassDef):
            constructor_session = _constructor_session(node)
            functions.extend(
                (node.name, constructor_session, item)
                for item in node.body
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                and item.name != "__init__"
            )
    for owner, constructor_session, function in functions:
        session = _session_parameter(function) or constructor_session
        if session is None:
            continue
        if isinstance(function, ast.FunctionDef):
            gaps.append(f"{relative}:{function.name}: AsyncSession repository must be async")
        transaction_calls = [
            call
            for call in ast.walk(function)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and _receiver_name(call.func.value) == session
            and call.func.attr in {"begin", "begin_nested"}
        ]
        if len(transaction_calls) > 1:
            gaps.append(f"{relative}:{function.name}: multiple transaction scopes require capture")
        operations = []
        for call in sorted(
            (item for item in ast.walk(function) if isinstance(item, ast.Call)),
            key=lambda item: (item.lineno, item.col_offset),
        ):
            if not (
                isinstance(call.func, ast.Attribute) and _receiver_name(call.func.value) == session
            ):
                continue
            name = call.func.attr
            if name in {"begin", "begin_nested"}:
                continue
            if name not in SESSION_OPERATIONS:
                gaps.append(
                    f"{relative}:{function.name}: unsupported AsyncSession operation {name}"
                )
                continue
            if any(keyword.arg is None for keyword in call.keywords):
                gaps.append(f"{relative}:{function.name}: dynamic AsyncSession keyword")
                continue
            operations.append(
                {
                    "name": name,
                    "arguments": [ast.unparse(argument) for argument in call.args],
                    "options": dict(
                        sorted(
                            (keyword.arg, ast.unparse(keyword.value))
                            for keyword in call.keywords
                            if keyword.arg is not None
                        )
                    ),
                }
            )
        transaction = (
            _call_name(transaction_calls[0].func).rsplit(".", 1)[-1]
            if transaction_calls
            else "manual"
            if any(operation["name"] in {"commit", "rollback"} for operation in operations)
            else "implicit"
        )
        for item in ast.walk(function):
            if isinstance(item, ast.If | ast.For | ast.AsyncFor | ast.While | ast.Try | ast.Match):
                gaps.append(
                    f"{relative}:{function.name}: conditional repository flow requires capture"
                )
                break
        result.append(
            {
                "module": relative,
                "owner": owner,
                "name": function.name,
                "async": isinstance(function, ast.AsyncFunctionDef),
                "session": session,
                "transaction": transaction,
                "operations": operations,
            }
        )
    return result


def capture_fastapi_persistence(
    root: Path, *, excluded: frozenset[str] = frozenset()
) -> dict[str, Any] | None:
    """Capture conventional FastAPI persistence declarations from regular Python files."""
    files = []
    pydantic_models = []
    sqlalchemy_models = []
    migrations = []
    repositories = []
    captured_imports: dict[str, list[dict[str, str | None]]] = {}
    gaps: list[str] = []
    for path in sorted(root.rglob("*.py"), key=lambda item: item.relative_to(root).as_posix()):
        relative_path = path.relative_to(root)
        if any(part in IGNORED for part in relative_path.parts):
            continue
        relative = relative_path.as_posix()
        if relative in excluded:
            continue
        if path.is_symlink() or not path.is_file():
            gaps.append(f"{relative}: persistence source must be a regular file")
            continue
        tree = ast.parse(path.read_text(), filename=relative)
        imports = {
            (node.module, alias.name, alias.asname)
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and not node.level
            for alias in node.names
        }
        recognized = False
        if ("pydantic", "BaseModel", None) in imports:
            pydantic_models.extend(_pydantic_models(relative_path, tree, gaps))
            recognized = True
        if ("sqlalchemy.orm", "DeclarativeBase", None) in imports:
            sqlalchemy_models.extend(_sqlalchemy_models(relative_path, tree, gaps))
            recognized = True
        if "versions" in relative_path.parts:
            revision = _migration(relative_path, tree, gaps)
            if revision is not None:
                migrations.append(revision)
                recognized = True
        if ("sqlalchemy.ext.asyncio", "AsyncSession", None) in imports:
            repositories.extend(_repositories(relative_path, tree, gaps))
            recognized = True
        if recognized:
            files.append(relative)
            module_imports: list[dict[str, str | None]] = []
            for node in tree.body:
                if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    module_imports.extend(
                        {
                            "module": node.module,
                            "name": alias.name,
                            "local": alias.asname or alias.name,
                        }
                        for alias in node.names
                    )
                elif isinstance(node, ast.Import):
                    module_imports.extend(
                        {
                            "module": alias.name,
                            "name": None,
                            "local": alias.asname or alias.name.split(".", 1)[0],
                        }
                        for alias in node.names
                    )
            captured_imports[relative] = sorted(
                module_imports,
                key=lambda item: (item["module"] or "", item["name"] or "", item["local"] or ""),
            )
    if not files:
        return None
    revisions = {item["revision"] for item in migrations}
    if len(revisions) != len(migrations):
        gaps.append("migration revisions must be unique")
    for migration in migrations:
        parents = migration["down_revision"]
        parents = parents if isinstance(parents, list) else [parents]
        for parent in parents:
            if parent is not None and parent not in revisions:
                gaps.append(f"{migration['module']}: unknown down_revision {parent}")
    return {
        "schema": "sanka.python-to-golang.fastapi-persistence/v1",
        "files": sorted(files),
        "imports": dict(sorted(captured_imports.items())),
        "pydantic_models": sorted(pydantic_models, key=lambda item: (item["module"], item["name"])),
        "sqlalchemy_models": sorted(
            sqlalchemy_models, key=lambda item: (item["module"], item["table"], item["name"])
        ),
        "migrations": sorted(migrations, key=lambda item: (item["module"], str(item["revision"]))),
        "repositories": sorted(
            repositories,
            key=lambda item: (item["module"], item["owner"] or "", item["name"]),
        ),
        "gaps": sorted(set(gaps)),
    }


def _migration_literal(text: str) -> Any:
    return ast.literal_eval(ast.parse(text, mode="eval").body)


def _migration_call(text: str, name: str) -> ast.Call:
    node = ast.parse(text, mode="eval").body
    if not isinstance(node, ast.Call) or _call_name(node.func) != f"sa.{name}":
        raise ValueError(f"expected static sa.{name}")
    return node


def _migration_column(text: str) -> dict[str, Any]:
    call = _migration_call(text, "Column")
    if len(call.args) < 2:
        raise ValueError("migration Column requires a name and type")
    name = identifier(ast.literal_eval(call.args[0]))
    type_node = call.args[1]
    if not isinstance(type_node, ast.Call) or not _call_name(type_node.func).startswith("sa."):
        raise ValueError("migration Column requires a static SQLAlchemy type")
    type_name = _call_name(type_node.func)[3:]
    if type_name not in SQLA_TYPES:
        raise ValueError(f"unsupported migration column type: {type_name}")
    sql_type = SQLA_TYPES[type_name][0]
    type_options = _literal_keywords(type_node)
    if type_name == "String" and len(type_node.args) == 1 and not type_options:
        sql_type += f"({ast.literal_eval(type_node.args[0])})"
    elif type_name == "Numeric" and len(type_node.args) == 2 and not type_options:
        sql_type += f"({ast.literal_eval(type_node.args[0])},{ast.literal_eval(type_node.args[1])})"
    elif type_name == "DateTime" and not type_node.args and type_options == {"timezone": True}:
        pass
    elif type_node.args or type_options:
        raise ValueError("unsupported migration type options")
    options = _literal_keywords(call)
    if set(options) - {"nullable", "primary_key", "unique", "autoincrement"}:
        raise ValueError("unsupported migration Column options")
    if any(type(value) is not bool for value in options.values()):
        raise ValueError("migration Column options must be boolean")
    foreign = [arg for arg in call.args[2:] if isinstance(arg, ast.Call)]
    if len(foreign) != len(call.args[2:]) or len(foreign) > 1:
        raise ValueError("unsupported migration Column constraint")
    reference = None
    if foreign:
        fk = foreign[0]
        if _call_name(fk.func) != "sa.ForeignKey" or len(fk.args) != 1:
            raise ValueError("only static ForeignKey constraints are qualified")
        target = ast.literal_eval(fk.args[0])
        if type(target) is not str or len(target.split(".")) != 2:
            raise ValueError("migration ForeignKey requires table.column")
        ref_table, ref_column = map(identifier, target.split("."))
        fk_options = _literal_keywords(fk)
        if set(fk_options) - {"ondelete", "deferrable", "initially"}:
            raise ValueError("unsupported migration ForeignKey options")
        if "deferrable" in fk_options and type(fk_options["deferrable"]) is not bool:
            raise ValueError("migration ForeignKey deferrable must be boolean")
        reference = {
            "table": ref_table,
            "column": ref_column,
            "on_delete": fk_options.get("ondelete", "NO ACTION"),
            "deferrable": fk_options.get("deferrable", False),
            "deferred": fk_options.get("initially", "IMMEDIATE") == "DEFERRED",
        }
        if fk_options.get("initially", "IMMEDIATE") not in {"IMMEDIATE", "DEFERRED"}:
            raise ValueError("unsupported migration ForeignKey timing")
    return {"name": name, "sql_type": sql_type, "options": options, "references": reference}


def lower_linear_migrations(
    persistence: dict[str, Any], models: list[dict[str, Any]], schema_mode: str
) -> list[dict[str, Any]]:
    """Validate a static, additive Alembic chain against the final model schema."""
    revisions = persistence["migrations"]
    if not revisions:
        return []
    if schema_mode != "empty":
        raise ValueError("Alembic history requires an empty target schema")
    by_revision = {item["revision"]: item for item in revisions}
    if len(by_revision) != len(revisions):
        raise ValueError("migration revisions must be unique")
    if any(
        type(item["revision"]) is not str
        or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", item["revision"])
        for item in revisions
    ):
        raise ValueError("migration revision must be a safe file identifier")
    roots = [item for item in revisions if item["down_revision"] is None]
    if len(roots) != 1:
        raise ValueError("migration history must have one linear root")
    children: dict[str, list[dict[str, Any]]] = {}
    for item in revisions:
        if item["branch_labels"] is not None or item["depends_on"] is not None:
            raise ValueError("branched or dependent migrations require lowering")
        if item["down_revision"] is not None:
            if type(item["down_revision"]) is not str or item["down_revision"] not in by_revision:
                raise ValueError("migration history must be linear")
            children.setdefault(item["down_revision"], []).append(item)
    ordered = []
    current = roots[0]
    while True:
        ordered.append(current)
        next_items = children.get(current["revision"], [])
        if len(next_items) > 1:
            raise ValueError("branched migrations require lowering")
        if not next_items:
            break
        current = next_items[0]
        if current in ordered:
            raise ValueError("cyclic migration history")
    if len(ordered) != len(revisions):
        raise ValueError("migration history is disconnected")
    by_table = {model["table"]: model for model in models}
    created: set[str] = set()
    indexes: set[tuple[str, tuple[str, ...]]] = set()
    index_names: set[str] = set()
    result = []
    for revision in ordered:
        imported = {
            (item["module"], item["name"], item["local"])
            for item in persistence["imports"][revision["module"]]
        }
        if {("alembic", "op", "op"), ("sqlalchemy", None, "sa")} - imported:
            raise ValueError("migration requires canonical Alembic and SQLAlchemy imports")
        steps = []
        for operation in revision["upgrade"]:
            name, args, options = operation["name"], operation["arguments"], operation["options"]
            if name == "create_table" and args and not options:
                table = identifier(_migration_literal(args[0]))
                model = by_table.get(table)
                if model is None or table in created:
                    raise ValueError("migration creates an unknown or duplicate table")
                columns = [_migration_column(arg) for arg in args[1:]]
                if [column["name"] for column in columns] != [
                    field["name"] for field in model["fields"]
                ]:
                    raise ValueError(f"migration columns differ from model {table}")
                for column, field in zip(columns, model["fields"], strict=True):
                    opts = column["options"]
                    if (
                        column["sql_type"] != field["sql_type"]
                        or opts.get("nullable", not opts.get("primary_key", False))
                        != field["nullable"]
                        or opts.get("primary_key", False) != field["primary_key"]
                        or opts.get("unique", False) != field["unique"]
                        or opts.get("autoincrement", field["auto"]) != field["auto"]
                        or column["references"] != field.get("references")
                    ):
                        raise ValueError(
                            f"migration column differs from model {table}.{field['name']}"
                        )
                    if field.get("default"):
                        raise ValueError("migration defaults require lowering")
                    if field.get("references") and field["references"]["table"] not in created:
                        raise ValueError("migration foreign key precedes its parent table")
                if model.get("constraints"):
                    raise ValueError("migration table constraints require lowering")
                created.add(table)
                steps.append({"name": name, "table": table})
            elif name == "create_index" and len(args) == 3 and set(options) <= {"unique"}:
                index = identifier(_migration_literal(args[0]))
                table = identifier(_migration_literal(args[1]))
                index_columns = _migration_literal(args[2])
                if (
                    table not in created
                    or type(index_columns) is not list
                    or not index_columns
                    or not all(type(column) is str for column in index_columns)
                    or _migration_literal(options.get("unique", "False")) is not False
                ):
                    raise ValueError("unsupported migration index")
                names = tuple(identifier(column) for column in index_columns)
                model = by_table[table]
                matching = any(
                    item["name"] == index and tuple(item["columns"]) == names
                    for item in model.get("indexes", [])
                ) or (
                    len(names) == 1
                    and index == f"ix_{table}_{names[0]}"
                    and any(
                        field["name"] == names[0] and field.get("index")
                        for field in model["fields"]
                    )
                )
                if not matching or (table, names) in indexes or index in index_names:
                    raise ValueError("migration index differs from model")
                indexes.add((table, names))
                index_names.add(index)
                steps.append({"name": name, "table": table, "index": index, "columns": list(names)})
            else:
                raise ValueError(f"unsupported Alembic operation: {name}")
        expected_down = [
            ("drop_table", step["table"], None)
            if step["name"] == "create_table"
            else ("drop_index", step["index"], step["table"])
            for step in reversed(steps)
        ]
        actual_down = []
        for item in revision["downgrade"]:
            if len(item["arguments"]) != 1:
                raise ValueError("migration downgrade must reverse its upgrade")
            actual_down.append(
                (
                    item["name"],
                    identifier(_migration_literal(item["arguments"][0])),
                    identifier(_migration_literal(item["options"]["table_name"]))
                    if item["options"] and set(item["options"]) == {"table_name"}
                    else None,
                )
            )
        if actual_down != expected_down:
            raise ValueError("migration downgrade must reverse its upgrade")
        result.append({"revision": revision["revision"], "steps": steps})
    if created != set(by_table):
        raise ValueError("migration history does not match the captured model tables")
    expected_indexes = {
        (model["table"], (field["name"],))
        for model in models
        for field in model["fields"]
        if field.get("index")
    } | {
        (model["table"], tuple(item["columns"]))
        for model in models
        for item in model.get("indexes", [])
    }
    if indexes != expected_indexes:
        raise ValueError("migration history does not match the captured model indexes")
    return result

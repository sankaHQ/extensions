# SPDX-License-Identifier: Apache-2.0
"""Generate native async SQL output and the benchmark-only Django projection."""

from __future__ import annotations

from typing import Any

SQL_ENGINES = ("tortoise", "sqlalchemy", "psycopg")
DEFAULT_SQL_ENGINE = "tortoise"
BENCH_DJANGO_ENGINE = "django"
SQL_ENGINE_LABELS = {
    "tortoise": "Tortoise ORM (recommended — closest to Django)",
    "sqlalchemy": "SQLAlchemy 2.0 async",
    "psycopg": "psycopg3 async (PostgreSQL only)",
}


def resolve_sql_engine(value: str | None) -> str:
    engine = (value or DEFAULT_SQL_ENGINE).strip().lower()
    if engine not in SQL_ENGINES:
        raise ValueError(f"unknown SQL engine: {engine}; choose {', '.join(SQL_ENGINES)}")
    return engine


def render_async_sql_files(
    output_write: Any,
    *,
    entrypoint: str,
    manifest: dict[str, Any],
    sql_engine: str,
    database_required: bool = True,
    module_prefix: str = "",
) -> list[str]:
    """Write store/runtime/models. Returns generated Python file names."""
    del entrypoint
    if sql_engine not in (*SQL_ENGINES, BENCH_DJANGO_ENGINE, "none"):
        raise ValueError(f"unknown SQL engine: {sql_engine}")
    vendor = str((manifest.get("database") or {}).get("vendor") or "")
    if sql_engine == "psycopg" and vendor != "postgresql":
        raise ValueError(
            "psycopg requires PostgreSQL; this project's database is " + (vendor or "unknown")
        )
    names = ["sanka_native.py"]
    runtime = _RUNTIME
    if database_required:
        store = _render_store(sql_engine)
        if module_prefix:
            store = store.replace(
                "import models as models_mod",
                f"from {module_prefix} import models as models_mod",
            )
            store = store.replace(
                'modules={"models": ["models"]}',
                f'modules={{"models": ["{module_prefix}.models"]}}',
            )
        output_write("sanka_store.py", store)
        names.append("sanka_store.py")
    else:
        runtime = runtime.replace("import sanka_store as store\n", "")
        runtime = runtime.replace(
            "Persistence is async SQL via sanka_store —\nDjango is not imported.",
            "No generated route requires persistence, so database helpers are omitted.",
        )
    if module_prefix:
        runtime = runtime.replace(
            "import sanka_store as store", f"from {module_prefix} import sanka_store as store"
        )
    if database_required and sql_engine not in ("psycopg", BENCH_DJANGO_ENGINE):
        output_write("models.py", _render_models(sql_engine, manifest))
        names.append("models.py")
    if database_required and sql_engine == BENCH_DJANGO_ENGINE:
        output_write("sanka_settings.py", _render_django_settings(manifest))
        names.append("sanka_settings.py")
    output_write("sanka_native.py", runtime)
    requirements = _render_requirements(sql_engine, vendor, database_required=database_required)
    output_write("requirements.txt", requirements)
    output_write("pyproject.toml", render_generated_pyproject(requirements))
    return names


def _ident(value: str) -> str:
    import re

    slug = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_")
    return slug or "Item"


def _models_spec(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {}
    for resource in manifest.get("resources", []):
        _add_model_spec(specs, resource)
        for field in resource.get("fields", []):
            if field.get("kind") != "nested_many" or not field.get("child"):
                continue
            child = field["child"]
            _add_model_spec(specs, child)
            fk = str(field.get("attname") or "")
            table = str(child.get("db_table") or "")
            if fk and table in specs:
                specs[table]["columns"].setdefault(
                    fk,
                    {
                        "name": fk,
                        "kind": "integer",
                        "pk": False,
                        "max_length": None,
                        "allow_null": False,
                        "has_default": False,
                        "default": None,
                    },
                )
    return [specs[key] for key in sorted(specs)]


def _add_model_spec(specs: dict[str, dict[str, Any]], item: dict[str, Any]) -> None:
    table = str(item.get("db_table") or "")
    if not table:
        return
    class_name = _ident(str(item.get("model_class") or item.get("object_name") or table))
    columns: dict[str, dict[str, Any]] = dict(specs.get(table, {}).get("columns") or {})
    pk = str(item.get("pk_attname") or "id")
    columns.setdefault(pk, {"name": pk, "kind": "integer", "pk": True, "max_length": None})
    for field in item.get("fields", []):
        if field.get("kind") == "nested_many":
            continue
        name = str(field.get("attname") or field["name"])
        columns[name] = {
            "name": name,
            "kind": field.get("kind") or "char",
            "pk": name == pk,
            "max_length": field.get("max_length"),
            "decimal_places": field.get("decimal_places"),
            "max_digits": field.get("max_digits"),
            "allow_null": bool(field.get("allow_null")),
            "has_default": bool(field.get("has_default")),
            "default": field.get("default"),
        }
    specs[table] = {
        "table": table,
        "class_name": class_name,
        "pk": pk,
        "ordering": list(item.get("ordering") or (pk,)),
        "columns": columns,
    }


def _render_models(sql_engine: str, manifest: dict[str, Any]) -> str:
    specs = _models_spec(manifest)
    if sql_engine == "tortoise":
        lines = [
            "# Generated by Sanka. Tortoise models mapped onto the existing Django tables.",
            "from tortoise import fields",
            "from tortoise.models import Model",
            "",
        ]
        for spec in specs:
            lines.append(f"class {spec['class_name']}(Model):")
            for column in spec["columns"].values():
                lines.append(f"    {column['name']} = {_tortoise_field(column)}")
            lines.append("    class Meta:")
            lines.append(f"        table = {spec['table']!r}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"
    lines = [
        "# Generated by Sanka. SQLAlchemy models mapped onto the existing Django tables.",
        "from sqlalchemy import DateTime, Integer, Numeric, String",
        "from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column",
        "",
        "class Base(DeclarativeBase):",
        "    pass",
        "",
    ]
    for spec in specs:
        lines.append(f"class {spec['class_name']}(Base):")
        lines.append(f"    __tablename__ = {spec['table']!r}")
        for column in spec["columns"].values():
            lines.append(f"    {column['name']}: Mapped[object] = {_sqlalchemy_column(column)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _tortoise_field(column: dict[str, Any]) -> str:
    if column.get("pk"):
        return "fields.IntField(primary_key=True)"
    kind = column["kind"]
    args: list[str] = []
    if column.get("allow_null"):
        args.append("null=True")
    if kind == "char" and column.get("has_default"):
        args.append(f"default={column.get('default')!r}")
    elif kind == "char":
        args.append('default=""')
    elif column.get("has_default") and column.get("default") is not None:
        args.append(f"default={column['default']!r}")
    extra = (", " + ", ".join(args)) if args else ""
    if kind in {"integer", "related_pk"}:
        wrapped = f"({extra.strip(', ')})" if extra else "()"
        return f"fields.IntField{wrapped}"
    if kind == "decimal":
        digits = int(column.get("max_digits") or 12)
        places = int(column.get("decimal_places") or 2)
        return f"fields.DecimalField(max_digits={digits}, decimal_places={places}{extra})"
    if kind == "datetime":
        wrapped = f"({extra.strip(', ')})" if extra else "()"
        return f"fields.DatetimeField{wrapped}"
    max_length = int(column.get("max_length") or 255)
    return f"fields.CharField(max_length={max_length}{extra})"


def _sqlalchemy_column(column: dict[str, Any]) -> str:
    if column.get("pk"):
        return "mapped_column(Integer, primary_key=True)"
    kind = column["kind"]
    nullable = "nullable=True" if column.get("allow_null") else "nullable=False"
    if kind in {"integer", "related_pk"}:
        return f"mapped_column(Integer, {nullable})"
    if kind == "decimal":
        digits = int(column.get("max_digits") or 12)
        places = int(column.get("decimal_places") or 2)
        return f"mapped_column(Numeric({digits}, {places}), {nullable})"
    if kind == "datetime":
        return f"mapped_column(DateTime(timezone=True), {nullable})"
    max_length = int(column.get("max_length") or 255)
    return f"mapped_column(String({max_length}), {nullable})"


def _render_requirements(sql_engine: str, vendor: str, *, database_required: bool = True) -> str:
    lines = ["fastapi>=0.115,<1", "uvicorn[standard]>=0.30,<1"]
    if not database_required:
        return "\n".join(lines) + "\n"
    if sql_engine == BENCH_DJANGO_ENGINE:
        lines.append("django>=5.2,<7")
    elif sql_engine == "tortoise":
        lines.append("tortoise-orm>=1.1,<2")
        lines.append("asyncpg>=0.29,<1" if vendor == "postgresql" else "aiosqlite>=0.20,<1")
    elif sql_engine == "sqlalchemy":
        lines.append("sqlalchemy[asyncio]>=2.0,<3")
        lines.append("asyncpg>=0.29,<1" if vendor == "postgresql" else "aiosqlite>=0.20,<1")
    else:
        lines.append("psycopg[binary]>=3.2,<4")
    return "\n".join(lines) + "\n"


def render_generated_pyproject(requirements: str) -> str:
    import json

    dependencies = "\n".join(f"  {json.dumps(line)}," for line in requirements.splitlines() if line)
    return f"""# Generated by Sanka. This project owns the target runtime dependencies.
[project]
name = "sanka-generated-fastapi"
version = "0.0.0"
requires-python = ">=3.12"
dependencies = [
{dependencies}
]

[project.optional-dependencies]
test = [
  "httpx>=0.27,<1",
  "httpx2>=2,<3",
]

[tool.uv]
package = false
"""


def _render_store(sql_engine: str) -> str:
    if sql_engine == BENCH_DJANGO_ENGINE:
        return _DJANGO_STORE
    if sql_engine == "tortoise":
        return _TORTOISE_STORE
    if sql_engine == "sqlalchemy":
        return _SQLALCHEMY_STORE
    return _PSYCOPG_STORE


def _render_django_settings(manifest: dict[str, Any]) -> str:
    settings_module = str(manifest["settings_module"])
    return f'''# Generated by Sanka under the license selected for this generated application.
"""Serving settings: the original settings without the DRF request layer."""

from {settings_module} import *  # noqa: F401,F403

INSTALLED_APPS = [
    app
    for app in INSTALLED_APPS  # noqa: F405
    if not app.startswith("rest_framework")
]
'''


_URL_HELPER = """
from pathlib import Path
import json
import os

HERE = Path(__file__).resolve().parent


def database_url() -> str:
    env = os.environ.get("SANKA_DATABASE_URL") or os.environ.get("SANKA_TEST_DB")
    captured = json.loads((HERE / "sanka-manifest.json").read_text(encoding="utf-8"))
    project_root = HERE.parents[1] if captured.get("generation_mode") == "full" else HERE
    database = captured.get("database") or {}
    vendor = str(database.get("vendor") or "sqlite")
    if env and "://" in env:
        return env
    if env and vendor == "sqlite":
        return _sqlite_url(env)
    name = str(database.get("name") or "")
    if vendor == "postgresql":
        raise RuntimeError(
            "set SANKA_DATABASE_URL for PostgreSQL (password is not stored in the scan)"
        )
    path = Path(name)
    if not path.is_absolute():
        path = (project_root / captured.get("source_root", ".")).resolve() / name
    return _sqlite_url(str(path))
"""


_DJANGO_STORE = r"""# Generated by Sanka. Async facade over the retained Django ORM.
from __future__ import annotations

import json
import os
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

from asgiref.sync import sync_to_async

HERE = Path(__file__).resolve().parent
MANIFEST = json.loads((HERE / "sanka-manifest.json").read_text(encoding="utf-8"))
PROJECT_ROOT = HERE.parents[1] if MANIFEST.get("generation_mode") == "full" else HERE
SOURCE_ROOT = (PROJECT_ROOT / MANIFEST.get("source_root", ".")).resolve()
for _entry in (str(HERE), str(SOURCE_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)
os.environ["DJANGO_SETTINGS_MODULE"] = "sanka_settings"

import django

django.setup()

_MODEL_CACHE: dict[tuple[str, str], Any] = {}
_USER_LOGIC = import_module("sanka_user_logic") if MANIFEST.get("has_user_logic") else None


async def init_db() -> None:
    return None


async def close_db() -> None:
    from django.db import connections

    await sync_to_async(connections.close_all, thread_sensitive=True)()


def _model(resource: dict[str, Any]) -> Any:
    key = (str(resource["model_module"]), str(resource["model_class"]))
    if key not in _MODEL_CACHE:
        module = import_module(key[0])
        _MODEL_CACHE[key] = getattr(module, key[1])
    return _MODEL_CACHE[key]


def _pk(resource: dict[str, Any]) -> str:
    return str(resource.get("pk_attname") or "id")


def _lookup(resource: dict[str, Any]) -> str:
    lookup = str(resource.get("lookup") or "pk")
    if lookup == "pk":
        return _pk(resource)
    for field in resource.get("fields") or ():
        if field.get("name") == lookup:
            return str(field.get("attname") or lookup)
    return lookup


def _fetch_all(resource: dict[str, Any]) -> list[Any]:
    query = _model(resource).objects.all()
    ordering = list(resource.get("ordering") or ())
    if ordering:
        query = query.order_by(*ordering)
    return list(query)


async def fetch_all(resource: dict[str, Any]) -> list[Any]:
    return await sync_to_async(_fetch_all, thread_sensitive=True)(resource)


def _fetch_one(resource: dict[str, Any], raw: Any) -> tuple[Any, str]:
    model = _model(resource)
    try:
        return model.objects.get(**{_lookup(resource): raw}), ""
    except model.DoesNotExist:
        return None, "missing"
    except (ValueError, TypeError, OverflowError):
        return None, "invalid"


async def fetch_one(resource: dict[str, Any], raw: Any) -> tuple[Any, str]:
    return await sync_to_async(_fetch_one, thread_sensitive=True)(resource, raw)


def _create_row(resource: dict[str, Any], data: dict[str, Any]) -> Any:
    return _model(resource).objects.create(**data)


async def create_row(resource: dict[str, Any], data: dict[str, Any]) -> Any:
    return await sync_to_async(_create_row, thread_sensitive=True)(resource, data)


def _create_with_user_logic(
    resource: dict[str, Any], data: dict[str, Any]
) -> tuple[Any, Any]:
    if _USER_LOGIC is None:
        raise RuntimeError("carried create logic is missing")
    create = resource.get("create") or {}
    function = getattr(_USER_LOGIC, str(create["function"]))
    try:
        return function(dict(data)), None
    except _USER_LOGIC.ValidationError as error:
        return None, error.detail


async def create_with_user_logic(
    resource: dict[str, Any], data: dict[str, Any]
) -> tuple[Any, Any]:
    return await sync_to_async(_create_with_user_logic, thread_sensitive=True)(resource, data)


def _save_row(instance: Any) -> None:
    instance.save()


async def save_row(_resource: dict[str, Any], instance: Any) -> None:
    await sync_to_async(_save_row, thread_sensitive=True)(instance)


def _delete_row(instance: Any) -> None:
    instance.delete()


async def delete_row(_resource: dict[str, Any], instance: Any) -> None:
    await sync_to_async(_delete_row, thread_sensitive=True)(instance)


def _unique_taken(
    resource: dict[str, Any], field: str, value: Any, exclude_pk: Any = None
) -> bool:
    query = _model(resource).objects.filter(**{field: value})
    if exclude_pk is not None:
        query = query.exclude(**{_pk(resource): exclude_pk})
    return query.exists()


async def unique_taken(
    resource: dict[str, Any], field: str, value: Any, exclude_pk: Any = None
) -> bool:
    return await sync_to_async(_unique_taken, thread_sensitive=True)(
        resource, field, value, exclude_pk
    )


def _fetch_children(spec: dict[str, Any], parent_id: Any) -> list[Any]:
    child = spec["child"]
    fk = spec.get("attname") or spec["name"]
    query = _model(child).objects.filter(**{fk: parent_id})
    ordering = list(child.get("ordering") or ())
    if ordering:
        query = query.order_by(*ordering)
    return list(query)


async def fetch_children(spec: dict[str, Any], parent_id: Any) -> list[Any]:
    return await sync_to_async(_fetch_children, thread_sensitive=True)(spec, parent_id)


def _token_user_id(auth: dict[str, Any], key: str) -> Any:
    from django.db import connection

    quote = connection.ops.quote_name
    query = (
        f"SELECT {quote(auth['token_user_column'])} "
        f"FROM {quote(auth['token_db_table'])} "
        f"WHERE {quote(auth['token_key_column'])} = %s"
    )
    with connection.cursor() as cursor:
        cursor.execute(query, [key])
        row = cursor.fetchone()
    return None if row is None else row[0]


async def token_user_id(auth: dict[str, Any], key: str) -> Any:
    return await sync_to_async(_token_user_id, thread_sensitive=True)(auth, key)


def _user_is_active(user_id: Any) -> bool | None:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.filter(pk=user_id).values_list("is_active", flat=True).first()


async def user_is_active(user_id: Any) -> bool | None:
    return await sync_to_async(_user_is_active, thread_sensitive=True)(user_id)
"""


_TORTOISE_STORE = (
    """# Generated by Sanka. Async store over Tortoise ORM (existing Django tables).
from __future__ import annotations

from typing import Any

from tortoise import Tortoise

"""
    + _URL_HELPER
    + """

def _sqlite_url(path: str) -> str:
    return "sqlite:///" + str(Path(path).resolve())


async def init_db() -> None:
    await Tortoise.init(
        db_url=database_url(),
        modules={"models": ["models"]},
        # FastAPI may run lifespan and request handlers in different tasks.
        _enable_global_fallback=True,
    )


async def close_db() -> None:
    await Tortoise.close_connections()


def _cls(resource: dict[str, Any]) -> Any:
    import models as models_mod

    return getattr(models_mod, str(resource["model_class"]))


def _pk(resource: dict[str, Any]) -> str:
    return str(resource.get("pk_attname") or "id")


def _lookup(resource: dict[str, Any]) -> tuple[str, str]:
    lookup = str(resource.get("lookup") or "pk")
    if lookup == "pk":
        return _pk(resource), "integer"
    for field in resource.get("fields") or ():
        if field.get("name") == lookup:
            return str(field.get("attname") or lookup), str(field.get("kind") or "char")
    return lookup, "char"


def _coerce_lookup(resource: dict[str, Any], raw: Any) -> tuple[Any, str]:
    if raw is None:
        return None, "invalid"
    _name, kind = _lookup(resource)
    if kind in {"integer", "related_pk"}:
        try:
            return int(str(raw)), ""
        except (TypeError, ValueError):
            return None, "invalid"
    return str(raw), ""


async def fetch_all(resource: dict[str, Any]) -> list[Any]:
    query = _cls(resource).all()
    ordering = list(resource.get("ordering") or ())
    if ordering:
        query = query.order_by(*ordering)
    return await query


async def fetch_one(resource: dict[str, Any], raw: Any) -> tuple[Any, str]:
    value, error = _coerce_lookup(resource, raw)
    if error:
        return None, error
    lookup, _kind = _lookup(resource)
    row = await _cls(resource).get_or_none(**{lookup: value})
    return (row, "") if row is not None else (None, "missing")


async def create_row(resource: dict[str, Any], data: dict[str, Any]) -> Any:
    return await _cls(resource).create(**data)


async def save_row(_resource: dict[str, Any], instance: Any) -> None:
    await instance.save()


async def delete_row(_resource: dict[str, Any], instance: Any) -> None:
    await instance.delete()


async def unique_taken(
    resource: dict[str, Any], field: str, value: Any, exclude_pk: Any = None
) -> bool:
    query = _cls(resource).filter(**{field: value})
    if exclude_pk is not None:
        query = query.exclude(**{_pk(resource): exclude_pk})
    return await query.exists()


async def fetch_children(spec: dict[str, Any], parent_id: Any) -> list[Any]:
    child = spec["child"]
    fk = spec.get("attname") or spec["name"]
    query = _cls(child).filter(**{fk: parent_id})
    ordering = list(child.get("ordering") or ())
    if ordering:
        query = query.order_by(*ordering)
    return await query


def _placeholder() -> str:
    return "?" if database_url().startswith("sqlite") else "$1"


async def token_user_id(auth: dict[str, Any], key: str) -> Any:
    conn = Tortoise.get_connection("default")
    mark = _placeholder()
    sql = (
        f'SELECT "{auth["token_user_column"]}" AS user_id '
        f'FROM "{auth["token_db_table"]}" WHERE "{auth["token_key_column"]}" = {mark}'
    )
    _, rows = await conn.execute_query(sql, [key])
    if not rows:
        return None
    return rows[0][0]


async def user_is_active(user_id: Any) -> bool | None:
    conn = Tortoise.get_connection("default")
    mark = _placeholder()
    _, rows = await conn.execute_query(
        f'SELECT "is_active" FROM "auth_user" WHERE "id" = {mark}', [user_id]
    )
    if not rows:
        return None
    return bool(rows[0][0])
"""
)


_SQLALCHEMY_STORE = (
    """# Generated by Sanka. Async store over SQLAlchemy (existing Django tables).
from __future__ import annotations

from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

"""
    + _URL_HELPER
    + """

def _sqlite_url(path: str) -> str:
    return "sqlite+aiosqlite:///" + str(Path(path).resolve())


_ENGINE = None
_SESSION = None


def _sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _ENGINE, _SESSION
    if _SESSION is None:
        url = database_url()
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        _ENGINE = create_async_engine(url)
        _SESSION = async_sessionmaker(_ENGINE, expire_on_commit=False)
    return _SESSION


async def init_db() -> None:
    _sessionmaker()


async def close_db() -> None:
    global _ENGINE, _SESSION
    if _ENGINE is not None:
        await _ENGINE.dispose()
    _ENGINE = None
    _SESSION = None


def _cls(resource: dict[str, Any]) -> Any:
    import models as models_mod

    return getattr(models_mod, str(resource["model_class"]))


def _pk(resource: dict[str, Any]) -> str:
    return str(resource.get("pk_attname") or "id")


def _lookup(resource: dict[str, Any]) -> tuple[str, str]:
    lookup = str(resource.get("lookup") or "pk")
    if lookup == "pk":
        return _pk(resource), "integer"
    for field in resource.get("fields") or ():
        if field.get("name") == lookup:
            return str(field.get("attname") or lookup), str(field.get("kind") or "char")
    return lookup, "char"


def _coerce_lookup(resource: dict[str, Any], raw: Any) -> tuple[Any, str]:
    if raw is None:
        return None, "invalid"
    _name, kind = _lookup(resource)
    if kind in {"integer", "related_pk"}:
        try:
            return int(str(raw)), ""
        except (TypeError, ValueError):
            return None, "invalid"
    return str(raw), ""


async def fetch_all(resource: dict[str, Any]) -> list[Any]:
    model = _cls(resource)
    stmt = select(model)
    for name in resource.get("ordering") or ():
        stmt = stmt.order_by(getattr(model, name))
    async with _sessionmaker()() as session:
        return list((await session.scalars(stmt)).all())


async def fetch_one(resource: dict[str, Any], raw: Any) -> tuple[Any, str]:
    value, error = _coerce_lookup(resource, raw)
    if error:
        return None, error
    model = _cls(resource)
    lookup, _kind = _lookup(resource)
    stmt = select(model).where(getattr(model, lookup) == value)
    async with _sessionmaker()() as session:
        row = await session.scalar(stmt)
    return (row, "") if row is not None else (None, "missing")


async def create_row(resource: dict[str, Any], data: dict[str, Any]) -> Any:
    model = _cls(resource)
    row = model(**data)
    async with _sessionmaker()() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def save_row(_resource: dict[str, Any], instance: Any) -> None:
    async with _sessionmaker()() as session:
        merged = await session.merge(instance)
        await session.commit()
        await session.refresh(merged)
        for key, value in vars(merged).items():
            if not key.startswith("_"):
                setattr(instance, key, value)


async def delete_row(_resource: dict[str, Any], instance: Any) -> None:
    async with _sessionmaker()() as session:
        merged = await session.merge(instance)
        await session.delete(merged)
        await session.commit()


async def unique_taken(
    resource: dict[str, Any], field: str, value: Any, exclude_pk: Any = None
) -> bool:
    model = _cls(resource)
    stmt = select(model).where(getattr(model, field) == value)
    if exclude_pk is not None:
        stmt = stmt.where(getattr(model, _pk(resource)) != exclude_pk)
    async with _sessionmaker()() as session:
        row = await session.scalar(stmt)
    return row is not None


async def fetch_children(spec: dict[str, Any], parent_id: Any) -> list[Any]:
    child = spec["child"]
    model = _cls(child)
    fk = spec.get("attname") or spec["name"]
    stmt = select(model).where(getattr(model, fk) == parent_id)
    for name in child.get("ordering") or ():
        stmt = stmt.order_by(getattr(model, name))
    async with _sessionmaker()() as session:
        return list((await session.scalars(stmt)).all())


async def token_user_id(auth: dict[str, Any], key: str) -> Any:
    sql = text(
        f'SELECT "{auth["token_user_column"]}" FROM "{auth["token_db_table"]}" '
        f'WHERE "{auth["token_key_column"]}" = :key'
    )
    async with _sessionmaker()() as session:
        result = await session.execute(sql, {"key": key})
        row = result.first()
    return None if row is None else row[0]


async def user_is_active(user_id: Any) -> bool | None:
    sql = text('SELECT "is_active" FROM "auth_user" WHERE "id" = :id')
    async with _sessionmaker()() as session:
        result = await session.execute(sql, {"id": user_id})
        row = result.first()
    if row is None:
        return None
    return bool(row[0])
"""
)


_PSYCOPG_STORE = (
    """# Generated by Sanka. Async store over psycopg3 (existing Django tables).
from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

"""
    + _URL_HELPER
    + """

def _sqlite_url(path: str) -> str:
    raise RuntimeError("psycopg does not support SQLite")


_CONNINFO = ""


async def init_db() -> None:
    global _CONNINFO
    _CONNINFO = os.environ.get("SANKA_DATABASE_URL") or ""
    if not _CONNINFO:
        raise RuntimeError("set SANKA_DATABASE_URL")


async def close_db() -> None:
    return None


def _pk(resource: dict[str, Any]) -> str:
    return str(resource.get("pk_attname") or "id")


def _lookup(resource: dict[str, Any]) -> tuple[str, str]:
    lookup = str(resource.get("lookup") or "pk")
    if lookup == "pk":
        return _pk(resource), "integer"
    for field in resource.get("fields") or ():
        if field.get("name") == lookup:
            return str(field.get("attname") or lookup), str(field.get("kind") or "char")
    return lookup, "char"


def _table(resource: dict[str, Any]) -> str:
    return str(resource["db_table"])


def _coerce_lookup(resource: dict[str, Any], raw: Any) -> tuple[Any, str]:
    if raw is None:
        return None, "invalid"
    _name, kind = _lookup(resource)
    if kind in {"integer", "related_pk"}:
        try:
            return int(str(raw)), ""
        except (TypeError, ValueError):
            return None, "invalid"
    return str(raw), ""


def _row(resource: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    return data


async def fetch_all(resource: dict[str, Any]) -> list[Any]:
    order = ", ".join(f'"{name}"' for name in (resource.get("ordering") or ("id",)))
    sql = f'SELECT * FROM "{_table(resource)}" ORDER BY {order}'
    async with await psycopg.AsyncConnection.connect(_CONNINFO) as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql)
            return list(await cur.fetchall())


async def fetch_one(resource: dict[str, Any], raw: Any) -> tuple[Any, str]:
    value, error = _coerce_lookup(resource, raw)
    if error:
        return None, error
    lookup, _kind = _lookup(resource)
    sql = f'SELECT * FROM "{_table(resource)}" WHERE "{lookup}" = %s'
    async with await psycopg.AsyncConnection.connect(_CONNINFO) as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (value,))
            row = await cur.fetchone()
    return (row, "") if row is not None else (None, "missing")


async def create_row(resource: dict[str, Any], data: dict[str, Any]) -> Any:
    cols = ", ".join(f'"{key}"' for key in data)
    placeholders = ", ".join(["%s"] * len(data))
    sql = (
        f'INSERT INTO "{_table(resource)}" ({cols}) VALUES ({placeholders}) RETURNING *'
    )
    async with await psycopg.AsyncConnection.connect(_CONNINFO) as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, tuple(data.values()))
            row = await cur.fetchone()
        await conn.commit()
    return row


async def save_row(resource: dict[str, Any], instance: Any) -> None:
    pk = _pk(resource)
    data = {key: value for key, value in dict(instance).items() if key != pk}
    assignments = ", ".join(f'"{key}" = %s' for key in data)
    sql = f'UPDATE "{_table(resource)}" SET {assignments} WHERE "{pk}" = %s'
    async with await psycopg.AsyncConnection.connect(_CONNINFO) as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (*data.values(), instance[pk]))
        await conn.commit()


async def delete_row(resource: dict[str, Any], instance: Any) -> None:
    pk = _pk(resource)
    sql = f'DELETE FROM "{_table(resource)}" WHERE "{pk}" = %s'
    async with await psycopg.AsyncConnection.connect(_CONNINFO) as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (instance[pk],))
        await conn.commit()


async def unique_taken(
    resource: dict[str, Any], field: str, value: Any, exclude_pk: Any = None
) -> bool:
    sql = f'SELECT 1 FROM "{_table(resource)}" WHERE "{field}" = %s'
    params: list[Any] = [value]
    if exclude_pk is not None:
        sql += f' AND "{_pk(resource)}" <> %s'
        params.append(exclude_pk)
    async with await psycopg.AsyncConnection.connect(_CONNINFO) as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchone() is not None


async def fetch_children(spec: dict[str, Any], parent_id: Any) -> list[Any]:
    child = spec["child"]
    fk = spec.get("attname") or spec["name"]
    order = ", ".join(f'"{name}"' for name in (child.get("ordering") or ("id",)))
    sql = f'SELECT * FROM "{child["db_table"]}" WHERE "{fk}" = %s ORDER BY {order}'
    async with await psycopg.AsyncConnection.connect(_CONNINFO) as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (parent_id,))
            return list(await cur.fetchall())


async def token_user_id(auth: dict[str, Any], key: str) -> Any:
    sql = (
        f'SELECT "{auth["token_user_column"]}" FROM "{auth["token_db_table"]}" '
        f'WHERE "{auth["token_key_column"]}" = %s'
    )
    async with await psycopg.AsyncConnection.connect(_CONNINFO) as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (key,))
            row = await cur.fetchone()
    return None if row is None else row[0]


async def user_is_active(user_id: Any) -> bool | None:
    async with await psycopg.AsyncConnection.connect(_CONNINFO) as conn:
        async with conn.cursor() as cur:
            await cur.execute('SELECT "is_active" FROM "auth_user" WHERE "id" = %s', (user_id,))
            row = await cur.fetchone()
    if row is None:
        return None
    return bool(row[0])
"""
)


_RUNTIME = r'''# Generated by Sanka under the license selected for this generated application.
"""Async FastAPI request layer. Validation is a native reimplementation of the
captured DRF serializer semantics. Persistence is async SQL via sanka_store —
Django is not imported.
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
import os
import re
import zoneinfo
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib import parse as _urlparse

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response

import sanka_store as store

HERE_MANIFEST = __import__("pathlib").Path(__file__).resolve().parent
MANIFEST = json.loads((HERE_MANIFEST / "sanka-manifest.json").read_text(encoding="utf-8"))

_DECIMAL_TAIL = re.compile(r"\.0*\s*$")
_MAX_STRING_LENGTH = 1000
_DEFAULT_MAX_REQUEST_BODY_BYTES = 1_048_576


def _attr(instance: Any, name: str) -> Any:
    if isinstance(instance, dict):
        return instance.get(name)
    return getattr(instance, name)


def _instance_pk(instance: Any) -> Any:
    if isinstance(instance, dict):
        return instance.get("id")
    return getattr(instance, "pk", None) or getattr(instance, "id")


def _set_attr(instance: Any, name: str, value: Any) -> None:
    if isinstance(instance, dict):
        instance[name] = value
    else:
        setattr(instance, name, value)


def resource(view: str) -> dict[str, Any]:
    for item in MANIFEST["resources"]:
        if item["view"] == view:
            return item
    raise KeyError(view)


async def read_raw_body(request: Request) -> bytes:
    try:
        maximum = int(os.environ.get("SANKA_MAX_REQUEST_BODY_BYTES", ""))
    except ValueError:
        maximum = _DEFAULT_MAX_REQUEST_BODY_BYTES
    if maximum <= 0:
        maximum = _DEFAULT_MAX_REQUEST_BODY_BYTES
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > maximum:
                raise HTTPException(status_code=413, detail="Request body too large.")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header.")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > maximum:
            raise HTTPException(status_code=413, detail="Request body too large.")
    return bytes(body)


def apply_security_headers(response: Response, request: Request, security: dict[str, Any]) -> None:
    if security.get("content_type_nosniff"):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
    if security.get("referrer_policy"):
        response.headers.setdefault("Referrer-Policy", security["referrer_policy"])
    if security.get("cross_origin_opener_policy"):
        response.headers.setdefault(
            "Cross-Origin-Opener-Policy", security["cross_origin_opener_policy"]
        )
    if security.get("x_frame_options"):
        response.headers.setdefault("X-Frame-Options", security["x_frame_options"])
    hsts_seconds = int(security.get("hsts_seconds") or 0)
    if hsts_seconds > 0 and request.url.scheme == "https":
        value = f"max-age={hsts_seconds}"
        if security.get("hsts_include_subdomains"):
            value += "; includeSubDomains"
        if security.get("hsts_preload"):
            value += "; preload"
        response.headers.setdefault("Strict-Transport-Security", value)


def _represent(spec: dict[str, Any], value: Any) -> Any:
    if spec["kind"] == "decimal" and value is not None:
        places = spec.get("decimal_places") or 0
        quantum = Decimal(1).scaleb(-places)
        return str(Decimal(value).quantize(quantum))
    if spec["kind"] == "datetime" and value is not None:
        aware = _as_aware_utc(value)
        zone = _field_zone(spec)
        shown = aware.astimezone(zone) if zone is not None else aware.replace(tzinfo=None)
        text = shown.isoformat()
        return text[:-6] + "Z" if text.endswith("+00:00") else text
    return value


async def _serialize_fields(fields: list[dict[str, Any]], instance: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for spec in fields:
        if spec["kind"] == "nested_many":
            child = spec["child"]
            related = await store.fetch_children(spec, _instance_pk(instance))
            payload[spec["name"]] = [
                await _serialize_fields(child["fields"], row) for row in related
            ]
        else:
            raw = _attr(instance, spec.get("attname") or spec["name"])
            payload[spec["name"]] = _represent(spec, raw)
    return payload


async def _serialize(resource: dict[str, Any], instance: Any) -> dict[str, Any]:
    return await _serialize_fields(resource["fields"], instance)


def _clean_integer(spec: dict[str, Any], value: Any) -> tuple[Any, list[str]]:
    messages = spec["messages"]
    if isinstance(value, str) and len(value) > _MAX_STRING_LENGTH:
        return None, [messages["max_string_length"]]
    try:
        cleaned = int(_DECIMAL_TAIL.sub("", str(value).strip()))
    except (TypeError, ValueError):
        return None, [messages["invalid"]]
    errors = []
    if spec.get("min_value") is not None and cleaned < spec["min_value"]:
        errors.append(messages["min_value"])
    if spec.get("max_value") is not None and cleaned > spec["max_value"]:
        errors.append(messages["max_value"])
    return cleaned, errors


def _clean_char(spec: dict[str, Any], value: Any) -> tuple[Any, list[str]]:
    messages = spec["messages"]
    if value == "" or (spec["trim_whitespace"] and isinstance(value, str) and not value.strip()):
        if spec["allow_blank"]:
            return "", []
        return None, [messages["blank"]]
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None, [messages["invalid"]]
    cleaned = str(value)
    if spec["trim_whitespace"]:
        cleaned = cleaned.strip()
    errors = []
    if spec.get("max_length") is not None and len(cleaned) > spec["max_length"]:
        errors.append(messages["max_length"])
    if spec.get("min_length") is not None and len(cleaned) < spec["min_length"]:
        errors.append(messages["min_length"])
    if "\x00" in cleaned and messages.get("null_characters"):
        errors.append(messages["null_characters"])
    if messages.get("surrogate_characters") and any(
        0xD800 <= ord(char) <= 0xDFFF for char in cleaned
    ):
        errors.append(messages["surrogate_characters"])
    return cleaned, errors


def _clean_decimal(spec: dict[str, Any], value: Any) -> tuple[Any, list[str]]:
    messages = spec["messages"]
    if isinstance(value, bool):
        return None, [messages["invalid"]]
    if isinstance(value, str) and len(value) > _MAX_STRING_LENGTH:
        return None, [messages["max_string_length"]]
    try:
        cleaned = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None, [messages["invalid"]]
    if cleaned.is_nan() or cleaned.is_infinite():
        return None, [messages["invalid"]]
    _sign, digittuple, exponent = cleaned.as_tuple()
    if not isinstance(exponent, int):
        return None, [messages["invalid"]]
    if exponent >= 0:
        digits = len(digittuple) + exponent
        decimals = 0
    elif abs(exponent) > len(digittuple):
        digits = decimals = abs(exponent)
    else:
        digits = len(digittuple)
        decimals = abs(exponent)
    whole_digits = digits - decimals
    max_digits = spec.get("max_digits")
    decimal_places = spec.get("decimal_places")
    if max_digits is not None and digits > max_digits:
        return None, [messages["max_digits"]]
    if decimal_places is not None and decimals > decimal_places:
        return None, [messages["max_decimal_places"]]
    if (
        max_digits is not None
        and decimal_places is not None
        and whole_digits > (max_digits - decimal_places)
    ):
        return None, [messages["max_whole_digits"]]
    return cleaned, []


def _clean_choice(spec: dict[str, Any], value: Any) -> tuple[Any, list[str]]:
    messages = spec["messages"]
    if value in spec["choices"]:
        return value, []
    return None, [messages["invalid_choice"].format(input=value)]


_DATETIME_RE = re.compile(
    r"(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})[T ](?P<hour>\d{1,2}):(?P<minute>\d{1,2})"
    r"(?::(?P<second>\d{1,2})(?:[.,](?P<microsecond>\d{1,6})\d{0,6})?)?"
    r"\s*(?P<tzinfo>Z|[+-]\d{2}(?::?\d{2})?)?$"
)
_SMART_SPLIT_RE = re.compile(
    r"""((?:[^\s'"]*(?:(?:"(?:[^"\\]|\\.)*" | '(?:[^'\\]|\\.)*')[^\s'"]*)+) | \S+)""",
    re.VERBOSE,
)
_ASCII_FOLD = str.maketrans({chr(code): chr(code + 32) for code in range(65, 91)})


def _parse_datetime(value: str) -> Any:
    """Django's parse_datetime: fromisoformat first, then the ISO 8601 regex."""
    try:
        return _dt.datetime.fromisoformat(value)
    except ValueError:
        match = _DATETIME_RE.match(value)
        if match is None:
            return None
        parts = match.groupdict()
        micro = parts.pop("microsecond")
        raw_zone = parts.pop("tzinfo")
        zone = None
        if raw_zone == "Z":
            zone = _dt.UTC
        elif raw_zone is not None:
            minutes = int(raw_zone[-2:]) if len(raw_zone) > 3 else 0
            offset = 60 * int(raw_zone[1:3]) + minutes
            if raw_zone[0] == "-":
                offset = -offset
            zone = _dt.timezone(_dt.timedelta(minutes=offset))
        numbers = {key: int(item) for key, item in parts.items() if item is not None}
        if micro:
            numbers["microsecond"] = int(micro.ljust(6, "0"))
        return _dt.datetime(**numbers, tzinfo=zone)


def _field_zone(spec: dict[str, Any]) -> Any:
    name = spec.get("timezone")
    return zoneinfo.ZoneInfo(name) if name else None


def _as_aware_utc(value: Any) -> Any:
    if isinstance(value, str):
        value = _parse_datetime(value)
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=_dt.UTC)
    return value.astimezone(_dt.UTC)


def _clean_datetime(spec: dict[str, Any], value: Any) -> tuple[Any, list[str]]:
    """DRF DateTimeField.to_internal_value for ISO 8601 input; values are kept in UTC."""
    messages = spec["messages"]
    if isinstance(value, bool) or not isinstance(value, str):
        return None, [messages["invalid"]]
    try:
        parsed = _parse_datetime(value)
    except ValueError:
        parsed = None
    if parsed is None:
        return None, [messages["invalid"]]
    zone = _field_zone(spec)
    if zone is None:
        if parsed.tzinfo is not None:
            return parsed.astimezone(_dt.UTC).replace(tzinfo=None), []
        return parsed, []
    if parsed.tzinfo is not None:
        try:
            return parsed.astimezone(_dt.UTC), []
        except OverflowError:
            return None, [messages["overflow"]]
    aware = parsed.replace(tzinfo=zone)
    if aware.astimezone(_dt.UTC).astimezone(zone).replace(tzinfo=None) != parsed:
        template = messages.get("make_aware") or (
            'Invalid datetime for the timezone "{timezone}".'
        )
        return None, [template.format(timezone=zone.key)]
    return aware.astimezone(_dt.UTC), []


def _fold(value: Any) -> str:
    """SQLite LIKE case folding: ASCII letters only."""
    return str(value).translate(_ASCII_FOLD)


def _search_terms(value: str) -> list[str]:
    """DRF search_smart_split: split on whitespace and commas, keep quoted phrases."""
    terms: list[str] = []
    for match in _SMART_SPLIT_RE.finditer(value):
        term = match[0].strip(",")
        if term.startswith(('"', "'")) and term[0] == term[-1]:
            quote = term[0]
            terms.append(term[1:-1].replace("\\" + quote, quote).replace("\\\\", "\\"))
        else:
            terms.extend(sub.strip() for sub in term.split(",") if sub)
    return terms


def _search_match(value: Any, lookup: str, term: str) -> bool:
    if value is None:
        return False
    haystack = _fold(value)
    needle = _fold(term)
    if lookup == "istartswith":
        return haystack.startswith(needle)
    if lookup == "iexact":
        return haystack == needle
    return needle in haystack


def _apply_search(
    resource: dict[str, Any], search: dict[str, Any], request: Request, rows: list[Any]
) -> list[Any]:
    terms = _search_terms(str(request.query_params.get(search["param"], "")))
    fields = search.get("fields") or []
    if not terms or not fields:
        return rows
    return [
        row
        for row in rows
        if all(
            any(
                _search_match(
                    _attr(row, _column_name(resource, spec["name"])), spec["lookup"], term
                )
                for spec in fields
            )
            for term in terms
        )
    ]


def _column_name(resource: dict[str, Any], name: str) -> str:
    if name == "pk":
        return str(resource.get("pk_attname") or "id")
    for field in resource["fields"]:
        if field["name"] == name:
            return str(field.get("attname") or name)
    return name


def _field_spec(resource: dict[str, Any], name: str) -> dict[str, Any] | None:
    for field in resource["fields"]:
        if field["name"] == name:
            return field
    return None


def _field_kind(resource: dict[str, Any], name: str) -> str:
    if name in {"pk", resource.get("pk_attname") or "id"}:
        return "integer"
    spec = _field_spec(resource, name)
    return str(spec["kind"]) if spec is not None else "char"


def _comparable(kind: str, value: Any) -> Any:
    if value is None:
        return None
    if kind == "datetime":
        return _as_aware_utc(value)
    if kind == "decimal":
        return Decimal(str(value))
    if kind in {"integer", "related_pk"}:
        return int(value)
    return str(value)


def _sort_rows(resource: dict[str, Any], rows: list[Any], ordering: list[str]) -> list[Any]:
    """ORDER BY semantics on fetched rows: stable, NULLs first ascending (SQLite)."""
    ordered = list(rows)
    for term in reversed(ordering):
        descending = term.startswith("-")
        name = term[1:] if descending else term
        column = _column_name(resource, name)
        kind = _field_kind(resource, name)

        def sort_key(row: Any, column: str = column, kind: str = kind) -> Any:
            value = _comparable(kind, _attr(row, column))
            return (0, 0) if value is None else (1, value)

        ordered.sort(key=sort_key, reverse=descending)
    return ordered


def _requested_ordering(config: dict[str, Any] | None, request: Request) -> list[str] | None:
    """DRF OrderingFilter.get_ordering plus the recognised tie-break idiom, if any."""
    if not config:
        return None
    params = request.query_params.get(config["param"])
    chosen: list[str] | None = None
    if params:
        valid = set(config["fields"])
        terms = [item.strip() for item in params.split(",")]
        kept = [term for term in terms if (term[1:] if term.startswith("-") else term) in valid]
        if kept:
            chosen = kept
    if chosen is None:
        default = config.get("default")
        chosen = list(default) if default else None
    rule = config.get("rule")
    if chosen and rule in {"append-pk-follow-last", "append-pk-asc"}:
        pk = str(config.get("pk") or "id")
        present = {term[1:] if term.startswith("-") else term for term in chosen}
        if pk not in present and "pk" not in present:
            if rule == "append-pk-follow-last" and chosen[-1].startswith("-"):
                chosen.append("-" + pk)
            else:
                chosen.append(pk)
    return chosen


def _positive_int(text: str, strict: bool = False, cutoff: Any = None) -> int:
    number = int(text)
    if number < 0 or (number == 0 and strict):
        raise ValueError()
    if cutoff:
        return min(number, cutoff)
    return number


def _replace_query_param(url: str, key: str, value: str) -> str:
    scheme, netloc, path, query, fragment = _urlparse.urlsplit(url)
    params = _urlparse.parse_qs(query, keep_blank_values=True)
    params[key] = [value]
    return _urlparse.urlunsplit(
        (scheme, netloc, path, _urlparse.urlencode(sorted(params.items()), doseq=True), fragment)
    )


def _position_text(resource: dict[str, Any], row: Any, ordering: list[str]) -> str:
    """DRF's cursor position: str() of the Django attribute behind the first ordering."""
    name = ordering[0].lstrip("-")
    kind = _field_kind(resource, name)
    value = _attr(row, _column_name(resource, name))
    if kind == "datetime":
        return str(_as_aware_utc(value))
    if kind == "decimal":
        spec = _field_spec(resource, name) or {}
        places = int(spec.get("decimal_places") or 0)
        return str(Decimal(str(value)).quantize(Decimal(1).scaleb(-places)))
    return str(value)


def _position_value(resource: dict[str, Any], name: str, text: str) -> Any:
    kind = _field_kind(resource, name)
    if kind == "datetime":
        return _as_aware_utc(text)
    if kind == "decimal":
        return Decimal(text)
    if kind in {"integer", "related_pk"}:
        return int(text)
    return text


def _encode_cursor(base_url: str, param: str, offset: int, reverse: bool, position: Any) -> str:
    tokens: dict[str, str] = {}
    if offset != 0:
        tokens["o"] = str(offset)
    if reverse:
        tokens["r"] = "1"
    if position is not None:
        tokens["p"] = str(position)
    querystring = _urlparse.urlencode(tokens, doseq=True)
    encoded = base64.b64encode(querystring.encode("ascii")).decode("ascii")
    return _replace_query_param(base_url, param, encoded)


def _cursor_links(
    resource: dict[str, Any], state: dict[str, Any], base_url: str, param: str
) -> tuple[Any, Any]:
    """DRF CursorPagination.get_next_link / get_previous_link, ported statement for statement."""
    page = state["page"]
    cursor = state["cursor"]
    ordering = state["ordering"]
    page_size = state["page_size"]
    next_link = None
    if state["has_next"]:
        if page and cursor and cursor["reverse"] and cursor["offset"] != 0:
            compare = _position_text(resource, page[-1], ordering)
        else:
            compare = state["next_position"]
        offset = 0
        position = None
        unique = False
        for item in reversed(page):
            position = _position_text(resource, item, ordering)
            if position != compare:
                unique = True
                break
            compare = position
            offset += 1
        if page and not unique:
            if not state["has_previous"]:
                offset, position = page_size, None
            elif cursor and cursor["reverse"]:
                offset, position = 0, state["previous_position"]
            else:
                base_offset = cursor["offset"] if cursor else 0
                offset, position = base_offset + page_size, state["previous_position"]
        if not page:
            position = state["next_position"]
        next_link = _encode_cursor(base_url, param, offset, False, position)
    previous_link = None
    if state["has_previous"]:
        if page and cursor and not cursor["reverse"] and cursor["offset"] != 0:
            compare = _position_text(resource, page[0], ordering)
        else:
            compare = state["previous_position"]
        offset = 0
        position = None
        unique = False
        for item in page:
            position = _position_text(resource, item, ordering)
            if position != compare:
                unique = True
                break
            compare = position
            offset += 1
        if page and not unique:
            if not state["has_next"]:
                offset, position = page_size, None
            elif cursor and cursor["reverse"]:
                base_offset = cursor["offset"] if cursor else 0
                offset, position = base_offset + page_size, state["next_position"]
            else:
                offset, position = 0, state["next_position"]
        if not page:
            position = state["previous_position"]
        previous_link = _encode_cursor(base_url, param, offset, True, position)
    return next_link, previous_link


def _cursor_page(
    resource: dict[str, Any],
    rows: list[Any],
    ordering: list[str],
    request: Request,
    config: dict[str, Any],
    allow: str,
) -> tuple[dict[str, Any] | None, Response | None]:
    """DRF CursorPagination.paginate_queryset over already-filtered rows."""
    page_size = config.get("page_size")
    size_param = config.get("page_size_param")
    if size_param and size_param in request.query_params:
        try:
            page_size = _positive_int(
                request.query_params[size_param], strict=True, cutoff=config.get("max_page_size")
            )
        except (KeyError, ValueError):
            page_size = config.get("page_size")
    if not page_size:
        return None, None
    param = str(config["cursor_param"])
    encoded = request.query_params.get(param)
    cursor: dict[str, Any] | None = None
    offset, reverse, position = 0, False, None
    if encoded is not None:
        try:
            querystring = base64.b64decode(encoded.encode("ascii")).decode("ascii")
            tokens = _urlparse.parse_qs(querystring, keep_blank_values=True)
            offset = _positive_int(
                tokens.get("o", ["0"])[0], cutoff=config.get("offset_cutoff") or 1000
            )
            reverse = bool(int(tokens.get("r", ["0"])[0]))
            position = tokens.get("p", [None])[0]
        except (TypeError, ValueError):
            return None, JSONResponse(
                {"detail": config["invalid_cursor_message"]},
                status_code=404,
                headers={"Allow": allow},
            )
        cursor = {"offset": offset, "reverse": reverse, "position": position}
    effective = [
        (term[1:] if term.startswith("-") else "-" + term) for term in ordering
    ] if reverse else list(ordering)
    ordered = _sort_rows(resource, rows, effective)
    if position is not None:
        first = ordering[0]
        is_reversed = first.startswith("-")
        name = first.lstrip("-")
        column = _column_name(resource, name)
        kind = _field_kind(resource, name)
        marker = _position_value(resource, name, position)
        keep_lower = reverse != is_reversed
        filtered = []
        for row in ordered:
            value = _comparable(kind, _attr(row, column))
            if value is None:
                continue
            if (value < marker) if keep_lower else (value > marker):
                filtered.append(row)
        ordered = filtered
    results = ordered[offset : offset + page_size + 1]
    page = results[: page_size]
    has_following = len(results) > len(page)
    following = _position_text(resource, results[-1], ordering) if has_following else None
    if reverse:
        page = list(reversed(page))
        has_next = (position is not None) or (offset > 0)
        has_previous = has_following
        next_position = position if has_next else None
        previous_position = following if has_previous else None
    else:
        has_next = has_following
        has_previous = (position is not None) or (offset > 0)
        next_position = following if has_next else None
        previous_position = position if has_previous else None
    state = {
        "page": page,
        "cursor": cursor,
        "ordering": ordering,
        "page_size": page_size,
        "has_next": has_next,
        "has_previous": has_previous,
        "next_position": next_position,
        "previous_position": previous_position,
    }
    next_link, previous_link = _cursor_links(resource, state, str(request.url), param)
    return {"page": page, "next": next_link, "previous": previous_link}, None


def _validate_scalar_fields(
    fields: list[dict[str, Any]], payload: Any, *, partial: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(payload, dict):
        kind = type(payload).__name__
        return {}, {
            "non_field_errors": [
                "Invalid data. Expected a dictionary, but got {0}.".format(kind)
            ]
        }
    validated: dict[str, Any] = {}
    errors: dict[str, Any] = {}
    for spec in fields:
        if spec["kind"] == "nested_many" or spec.get("read_only"):
            continue
        name = spec["name"]
        if name not in payload:
            if spec["required"] and not partial:
                errors[name] = [spec["messages"]["required"]]
            elif spec.get("has_default") and not partial:
                validated[spec.get("attname") or name] = spec["default"]
            continue
        raw = payload[name]
        if raw is None:
            if spec["allow_null"]:
                validated[spec.get("attname") or name] = None
            else:
                errors[name] = [spec["messages"]["null"]]
            continue
        cleaner = {
            "integer": _clean_integer,
            "char": _clean_char,
            "decimal": _clean_decimal,
            "choice": _clean_choice,
            "datetime": _clean_datetime,
            "related_pk": _clean_integer,
        }.get(spec["kind"])
        if cleaner is None:
            errors[name] = ["This field is not supported."]
            continue
        cleaned, field_errors = cleaner(spec, raw)
        if field_errors:
            errors[name] = field_errors
        else:
            validated[spec.get("attname") or name] = cleaned
    return validated, errors


async def _validate(
    resource: dict[str, Any],
    payload: Any,
    *,
    partial: bool,
    instance: Any,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}, {}, {"non_field_errors": [
            "Invalid data. Expected a dictionary, but got {0}.".format(type(payload).__name__)
        ]}
    validated, errors = _validate_scalar_fields(resource["fields"], payload, partial=partial)
    nested: dict[str, Any] = {}
    for spec in resource["fields"]:
        name = spec["name"]
        if spec["kind"] != "nested_many" or spec.get("read_only"):
            continue
        if name not in payload:
            if spec["required"] and not partial:
                errors[name] = [spec["messages"]["required"]]
            continue
        raw = payload[name]
        if raw is None:
            errors[name] = [spec["messages"]["null"]]
            continue
        if not isinstance(raw, list):
            template = spec["messages"]["not_a_list"]
            errors[name] = {
                "non_field_errors": [template.format(input_type=type(raw).__name__)]
            }
            continue
        item_errors: dict[str, Any] = {}
        items: list[dict[str, Any]] = []
        for index, raw_item in enumerate(raw):
            child_validated, child_errors = _validate_scalar_fields(
                spec["child"]["fields"], raw_item, partial=False
            )
            if child_errors:
                item_errors[str(index)] = child_errors
            else:
                items.append(child_validated)
        if item_errors:
            errors[name] = item_errors
        else:
            nested[name] = items
    for spec in resource["fields"]:
        column = spec.get("attname") or spec["name"]
        if spec.get("unique") and column in validated and validated[column] is not None:
            exclude = None if instance is None else _attr(
                instance, resource.get("pk_attname") or "id"
            )
            if await store.unique_taken(resource, column, validated[column], exclude):
                errors[spec["name"]] = [spec["unique_message"]]
                validated.pop(column, None)
    return validated, nested, errors


def _parse_json(raw: bytes) -> tuple[Any, Response | None]:
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return None, JSONResponse({"detail": f"JSON parse error - {exc}"}, status_code=400)


def _not_found(resource: dict[str, Any], allow: str, cause: str) -> Response:
    if cause == "missing":
        detail = f"No {resource['object_name']} matches the given query."
    else:
        detail = MANIFEST["generic_messages"]["not_found"]
    return JSONResponse({"detail": detail}, status_code=404, headers={"Allow": allow})


def _auth_error(message: str, auth: dict[str, Any], allow: str) -> Response:
    return JSONResponse(
        {"detail": message},
        status_code=401,
        headers={"Allow": allow, "WWW-Authenticate": auth["messages"]["www_authenticate"]},
    )


async def _authenticate(request: Request, auth: dict[str, Any], allow: str) -> tuple[Any, Any]:
    messages = auth["messages"]
    header = request.headers.get("authorization", "")
    parts = header.split()
    if not parts or parts[0].lower() != auth["token_keyword"].lower():
        return None, None
    if len(parts) == 1:
        return None, _auth_error(messages["empty_header"], auth, allow)
    if len(parts) > 2:
        return None, _auth_error(messages["spaced_header"], auth, allow)
    user_id = await store.token_user_id(auth, parts[1])
    if user_id is None:
        return None, _auth_error(messages["invalid_token"], auth, allow)
    is_active = await store.user_is_active(user_id)
    if is_active is None:
        return None, _auth_error(messages["invalid_token"], auth, allow)
    if not is_active:
        return None, _auth_error(messages["inactive_user"], auth, allow)
    return user_id, None


async def _require_user(request: Request, auth: dict[str, Any], allow: str) -> tuple[Any, Any]:
    user_id, error = await _authenticate(request, auth, allow)
    if error is not None:
        return None, error
    if user_id is None:
        return None, _auth_error(auth["messages"]["no_credentials"], auth, allow)
    return user_id, None


def _forbidden(auth: dict[str, Any], allow: str) -> Response:
    return JSONResponse(
        {"detail": auth["messages"]["forbidden"]}, status_code=403, headers={"Allow": allow}
    )


async def handle(
    spec: dict[str, Any],
    operation: str,
    request: Request,
) -> Any:
    path = next(route["path"] for route in spec["routes"] if route["operation"] == operation)
    allow = MANIFEST["allow"][path]
    auth = spec.get("auth")
    user_id = None
    if auth is not None:
        user_id, gate_error = await _require_user(request, auth, allow)
        if gate_error is not None:
            return gate_error
    if operation == "list":
        rows = await store.fetch_all(spec)
        listing = spec.get("listing") or {}
        if listing.get("search"):
            rows = _apply_search(spec, listing["search"], request, rows)
        ordering = _requested_ordering(listing.get("ordering"), request)
        pagination = listing.get("pagination")
        if pagination and pagination.get("kind") == "cursor":
            page_ordering = list(ordering) if ordering else list(pagination["ordering"])
            paged, page_error = _cursor_page(spec, rows, page_ordering, request, pagination, allow)
            if page_error is not None:
                return page_error
            if paged is not None:
                results = [await _serialize(spec, item) for item in paged["page"]]
                return JSONResponse(
                    {"next": paged["next"], "previous": paged["previous"], "results": results},
                    headers={"Allow": allow},
                )
        if ordering:
            rows = _sort_rows(spec, rows, list(ordering))
        payload = [await _serialize(spec, item) for item in rows]
        return JSONResponse(payload, headers={"Allow": allow})
    if operation == "retrieve":
        instance, miss = await store.fetch_one(spec, request.path_params.get(spec["lookup"]))
        if instance is None:
            return _not_found(spec, allow, miss)
        return JSONResponse(await _serialize(spec, instance), headers={"Allow": allow})
    if operation == "destroy":
        instance, miss = await store.fetch_one(spec, request.path_params.get(spec["lookup"]))
        if instance is None:
            return _not_found(spec, allow, miss)
        if (
            auth is not None
            and auth.get("owner_attname")
            and _attr(instance, auth["owner_attname"]) != user_id
        ):
            return _forbidden(auth, allow)
        for field in spec["fields"]:
            if field["kind"] == "nested_many":
                for child in await store.fetch_children(field, _instance_pk(instance)):
                    await store.delete_row(field["child"], child)
        await store.delete_row(spec, instance)
        return Response(status_code=204, headers={"Allow": allow})
    raw_body = await read_raw_body(request)
    payload, parse_error = _parse_json(raw_body)
    if parse_error is not None:
        parse_error.headers["Allow"] = allow
        return parse_error
    if operation == "create":
        validated, nested, errors = await _validate(spec, payload, partial=False, instance=None)
        if errors:
            return JSONResponse(errors, status_code=400, headers={"Allow": allow})
        if auth is not None and auth.get("inject_owner_attname"):
            validated[auth["inject_owner_attname"]] = user_id
        create = spec.get("create") or {"style": "default"}
        if create["style"] == "carryover":
            validated.update(nested)
            instance, carryover_error = await store.create_with_user_logic(spec, validated)
            if carryover_error is not None:
                return JSONResponse(carryover_error, status_code=400, headers={"Allow": allow})
        else:
            instance = await store.create_row(spec, validated)
            for field in spec["fields"]:
                if field["kind"] == "nested_many" and field["name"] in nested:
                    fk = field.get("attname") or field["name"]
                    parent_id = _attr(instance, spec.get("pk_attname") or "id")
                    for child in nested[field["name"]]:
                        child[fk] = parent_id
                        await store.create_row(field["child"], child)
        return JSONResponse(
            await _serialize(spec, instance), status_code=201, headers={"Allow": allow}
        )
    instance, miss = await store.fetch_one(spec, request.path_params.get(spec["lookup"]))
    if instance is None:
        return _not_found(spec, allow, miss)
    if (
        auth is not None
        and auth.get("owner_attname")
        and _attr(instance, auth["owner_attname"]) != user_id
    ):
        return _forbidden(auth, allow)
    validated, _nested, errors = await _validate(
        spec, payload, partial=(operation == "partial_update"), instance=instance
    )
    if errors:
        return JSONResponse(errors, status_code=400, headers={"Allow": allow})
    for name, value in validated.items():
        _set_attr(instance, name, value)
    await store.save_row(spec, instance)
    return JSONResponse(await _serialize(spec, instance), headers={"Allow": allow})


async def api_root(request: Request, path: str) -> Any:
    root = next(item for item in MANIFEST["api_roots"] if item["path"] == path)
    allow = MANIFEST["allow"][path]
    allowed_hosts = MANIFEST.get("http_security", {}).get("allowed_hosts", [])
    if allowed_hosts and "*" not in allowed_hosts:
        base = str(request.base_url).rstrip("/")
        payload = {key: f"{base}{link}" for key, link in root["links"]}
    else:
        payload = {key: link for key, link in root["links"]}
    return JSONResponse(payload, headers={"Allow": allow})


def _allow_for_path(path: str) -> str | None:
    for pattern, allow in MANIFEST["allow"].items():
        regex = "^" + re.sub(r"\\\{[^}\\\\]*\\\}", "[^/]+", re.escape(pattern)) + "$"
        if re.match(regex, path):
            return allow
    return None


def method_not_allowed(request: Request) -> Response:
    """DRF's 405: its detail template and the Allow header in http_method_names order."""
    template = MANIFEST.get("generic_messages", {}).get("method_not_allowed") or (
        'Method "{method}" not allowed.'
    )
    allow = _allow_for_path(request.url.path)
    headers = {"Allow": allow} if allow else {}
    return JSONResponse(
        {"detail": template.format(method=request.method)}, status_code=405, headers=headers
    )


async def options_response(request: Request, path: str) -> Any:
    """DRF's OPTIONS metadata: the variant the caller earns, PUT only for a reachable object."""
    spec = MANIFEST["options"][path]
    allow = MANIFEST["allow"][path]
    resource_spec = next(
        (
            item
            for item in MANIFEST["resources"]
            if any(route["path"] == path for route in item["routes"])
        ),
        None,
    )
    auth = resource_spec.get("auth") if resource_spec is not None else None
    user_id = None
    variant = spec["anonymous"]
    if auth is not None:
        user_id, gate_error = await _require_user(request, auth, allow)
        if gate_error is not None:
            return gate_error
        variant = spec["authorized"]
    body = json.loads(json.dumps(variant))
    actions = body.get("actions")
    if isinstance(actions, dict) and "PUT" in actions:
        keep = False
        if resource_spec is not None:
            instance, _miss = await store.fetch_one(
                resource_spec, request.path_params.get(resource_spec["lookup"])
            )
            keep = instance is not None
            owner_attname = auth.get("owner_attname") if auth is not None else None
            if keep and owner_attname and _attr(instance, owner_attname) != user_id:
                keep = False
        if not keep:
            actions.pop("PUT")
        if not actions:
            body.pop("actions")
    return JSONResponse(body, headers={"Allow": allow})
'''

# SPDX-License-Identifier: Apache-2.0
"""Generate bound-SQL membership helpers; no customer Python is emitted."""

from __future__ import annotations


def store_helpers(engine: str) -> str:
    adapters = {
        "tortoise": """
async def access_sql(sql, values, *, write=False):
    conn = Tortoise.get_connection("default")
    sqlite = conn.capabilities.dialect == "sqlite"
    for i in range(len(values)):
        sql = sql.replace("@p" + str(i) + "@", "?" + str(i + 1) if sqlite else "$" + str(i + 1))
    values = [_access_value(value, sqlite) for value in values]
    return await conn.execute_query_dict(sql, values)
""",
        "sqlalchemy": """
async def access_sql(sql, values, *, write=False):
    sqlite = database_url().startswith("sqlite")
    for i in range(len(values)):
        sql = sql.replace("@p" + str(i) + "@", ":p" + str(i))
    params = {"p" + str(i): _access_value(value, sqlite) for i, value in enumerate(values)}
    async with _sessionmaker()() as session:
        result = await session.execute(text(sql), params)
        rows = list(result.mappings()) if result.returns_rows else []
        if write:
            await session.commit()
        return rows
""",
        "psycopg": """
async def access_sql(sql, values, *, write=False):
    for i in range(len(values)):
        sql = sql.replace("@p" + str(i) + "@", "%(p" + str(i) + ")s")
    values = {"p" + str(i): value for i, value in enumerate(values)}
    async with await psycopg.AsyncConnection.connect(_CONNINFO) as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, values)
            return list(await cur.fetchall()) if cur.description else []
""",
        "django": """
def _access_sql(sql, values, write):
    from django.db import connection
    for i in range(len(values)):
        sql = sql.replace("@p" + str(i) + "@", "%s")
    with connection.cursor() as cursor:
        cursor.execute(sql, [_access_value(v, connection.vendor == "sqlite") for v in values])
        if not cursor.description:
            return []
        columns = [c[0] for c in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

async def access_sql(sql, values, *, write=False):
    return await sync_to_async(_access_sql, thread_sensitive=True)(sql, values, write)
""",
    }
    return """

def _access_value(value, sqlite):
    from uuid import UUID
    from datetime import datetime, timezone
    if sqlite and isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None).isoformat(" ")
    if isinstance(value, UUID):
        return value.hex if sqlite else value
    return value


def quote_identifier(value):
    return '"' + str(value).replace('"', '""') + '"'

async def access_user(auth, user_id):
    spec = auth.get("user") or {
        "table":"auth_user", "pk":"id", "active":"is_active", "superuser":"is_superuser"
    }
    q = quote_identifier
    rows = await access_sql(
        f'SELECT {q(spec["active"])} AS active, {q(spec["superuser"])} AS superuser '
        f'FROM {q(spec["table"])} WHERE {q(spec["pk"])} = @p0@', [user_id])
    return rows[0] if rows else None
""" + adapters[engine]


RUNTIME = r"""


def _access_request(spec, request):
    patterns = (spec.get("access") or {}).get("parameter_regexes") or {}
    if not patterns:
        return request
    for route in spec.get("routes", ()):
        pattern = re.escape(route["path"])
        for name, value in patterns.items():
            pattern = pattern.replace(
                re.escape("{" + name + "}"), "(?P<" + name + ">" + value + ")")
        match = re.fullmatch(pattern, request.url.path)
        if match:
            scope = {**request.scope, "path_params":{**request.path_params, **match.groupdict()}}
            return Request(scope, receive=request.receive)
    return request


def _access_key(value):
    from uuid import UUID
    if isinstance(value, UUID):
        return value.hex
    if isinstance(value, str):
        try:
            return UUID(value).hex
        except ValueError:
            pass
    return str(value)


def _relation_key(relation, raw):
    from uuid import UUID
    try:
        return UUID(str(raw)) if relation["pk_kind"] == "uuid" else int(raw)
    except (ValueError, TypeError, OverflowError):
        return None


async def _member_parents(relation, user_id):
    if user_id is None:
        return set()
    q = store.quote_identifier
    rows = await store.access_sql(
        f'SELECT {q(relation["parent_column"])} AS parent FROM {q(relation["table"])} '
        f'WHERE {q(relation["user_column"])} = @p0@', [user_id])
    return {_access_key(row["parent"]) for row in rows}


async def _scope_rows(spec, request, user_id, rows):
    query = (spec.get("access") or {}).get("query") or {}
    for rule in query.get("filters", ()):
        if rule["kind"] == "member":
            relation = rule["relation"]
            allowed = await _member_parents(relation, user_id)
            column = relation.get("foreign_key") or spec.get("pk_attname") or "id"
            rows = [row for row in rows if _access_key(_attr(row, column)) in allowed]
        elif rule["kind"] == "equal":
            value = rule["value"]
            expected = (request.path_params.get(value["name"])
                        if value["kind"] == "kwarg" else value["value"])
            rows = [row for row in rows
                    if _access_key(_attr(row, rule["field"])) == _access_key(expected)]
        else:
            raise RuntimeError("Unsupported scoped query contract")
    return rows


async def _access_permission(spec, request, auth, user_id, allow, instance=None):
    permission = (spec.get("access") or {}).get("permission")
    if not permission:
        return None
    parent_param = permission.get("parent_param")
    if (instance is None) != bool(parent_param):
        return None
    user = await store.access_user(auth, user_id) if user_id is not None else None
    if user and user["superuser"]:
        return None
    relation = permission["relation"]
    if parent_param:
        parent = _relation_key(relation, request.path_params.get(parent_param))
        q = store.quote_identifier
        rows = [] if parent is None else await store.access_sql(
            f'SELECT {q(relation["pk"])} FROM {q(relation["db_table"])} '
            f'WHERE {q(relation["pk"])} = @p0@', [parent])
        if not rows:
            return _not_found(relation, allow, "missing")
    else:
        column = relation.get("foreign_key") or spec.get("pk_attname") or "id"
        parent = _attr(instance, column)
    if _access_key(parent) in await _member_parents(relation, user_id):
        return None
    if user_id is None:
        return _auth_error(auth["messages"]["no_credentials"], auth, allow)
    return _forbidden(auth, allow)


async def _accessible_instance(spec, request, user_id):
    instance, miss = await store.fetch_one(spec, _lookup_value(spec, request))
    if instance is not None and not await _scope_rows(spec, request, user_id, [instance]):
        return None, "missing"
    return instance, miss


async def _add_creator_member(spec, instance, user_id):
    relation = (spec.get("access") or {}).get("creator_membership")
    if not relation:
        return
    if user_id is None:
        raise RuntimeError("Creator membership requires authentication")
    q = store.quote_identifier
    await store.access_sql(
        f'INSERT INTO {q(relation["table"])} '
        f'({q(relation["parent_column"])}, {q(relation["user_column"])}) '
        'VALUES (@p0@, @p1@) ON CONFLICT DO NOTHING',
        [_attr(instance, spec.get("pk_attname") or "id"), user_id], write=True)


async def _parent_create_values(spec, contract, request, validated, allow):
    parent = _relation_key(contract["parent"], request.path_params.get(contract["param"]))
    q = store.quote_identifier
    rows = [] if parent is None else await store.access_sql(
        f'SELECT {q(contract["parent"]["pk"])} FROM {q(contract["parent"]["db_table"])} '
        f'WHERE {q(contract["parent"]["pk"])} = @p0@', [parent])
    if not rows:
        raise RuntimeError("Parent disappeared before create")
    rows = await store.access_sql(
        f'SELECT 1 FROM {q(spec["db_table"])} WHERE {q(contract["column"])} = @p0@ '
        f'AND {q(contract["name"])} = @p1@ AND {q(contract["flag"])} = @p2@',
        [parent, validated[contract["name"]], False])
    if rows:
        return {}, JSONResponse([contract["message"]], status_code=400, headers={"Allow":allow})
    return {**validated, contract["column"]:parent}, None


async def _member_action(spec, request, auth, user_id, allow):
    contract = spec["access"]["member_action"]
    instance, miss = await _accessible_instance(spec, request, user_id)
    if instance is None:
        return _not_found(spec, allow, miss)
    payload, parse_error = _parse_json(await read_raw_body(request))
    if parse_error is not None:
        parse_error.headers["Allow"] = allow
        return parse_error
    permission_error = await _access_permission(spec, request, auth, user_id, allow, instance)
    if permission_error is not None:
        return permission_error
    name = contract["field"]
    messages = contract["messages"]
    def invalid(errors):
        return JSONResponse({name: errors}, status_code=400, headers={"Allow":allow})
    if not isinstance(payload, dict):
        return JSONResponse({"non_field_errors":[
            "Invalid data. Expected a dictionary, but got {0}.".format(type(payload).__name__)
        ]}, status_code=400, headers={"Allow":allow})
    if name not in payload:
        if contract["required"]:
            return invalid([messages["required"]])
        raise RuntimeError("Member update omitted its input field")
    values = payload[name]
    if values is None:
        return invalid([messages["null"]])
    if not isinstance(values, list):
        return invalid([messages["not_a_list"].format(input_type=type(values).__name__)])
    if not values and not contract["allow_empty"]:
        return invalid([messages["empty"]])
    q = store.quote_identifier
    user = contract["user"]
    validated = []
    for value in values:
        try:
            if isinstance(value, bool):
                raise TypeError()
            if not isinstance(value, (str, int, float)):
                raise TypeError()
            key = int(value)
            if key < -(2**63) or key >= 2**63:
                rows = []
            else:
                rows = await store.access_sql(
                    f'SELECT {q(user["pk"])} AS id FROM {q(user["table"])} '
                    f'WHERE {q(user["pk"])} = @p0@', [key])
        except (TypeError, ValueError, OverflowError):
            return invalid([contract["child_messages"]["incorrect_type"].format(
                data_type=type(value).__name__)])
        if not rows:
            return invalid([contract["child_messages"]["does_not_exist"].format(pk_value=value)])
        validated.append(rows[0]["id"])
    relation = contract["relation"]
    parent = _attr(instance, spec.get("pk_attname") or "id")
    # DRF validates the complete member list before the source loop starts.
    for member in validated:
        if contract["action"] == "add":
            sql = (f'INSERT INTO {q(relation["table"])} '
                f'({q(relation["parent_column"])}, {q(relation["user_column"])}) '
                'VALUES (@p0@, @p1@) ON CONFLICT DO NOTHING')
        else:
            sql = (f'DELETE FROM {q(relation["table"])} '
                f'WHERE {q(relation["parent_column"])} = @p0@ '
                f'AND {q(relation["user_column"])} = @p1@')
        await store.access_sql(sql, [parent, member], write=True)
        if contract["touches"]:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            assignments = ', '.join(f'{q(column)} = @p0@' for column in contract["touches"])
            await store.access_sql(
                f'UPDATE {q(spec["db_table"])} SET {assignments} '
                f'WHERE {q(spec.get("pk_attname") or "id")} = @p1@', [now,parent], write=True)
    rows = await store.access_sql(
        f'SELECT u.{q(user["pk"])} AS id FROM {q(user["table"])} u '
        f'JOIN {q(relation["table"])} m ON m.{q(relation["user_column"])} = u.{q(user["pk"])} '
        f'WHERE m.{q(relation["parent_column"])} = @p0@', [parent])
    return JSONResponse({name:[row["id"] for row in rows]}, headers={"Allow":allow})


async def _access_representation(field, instance):
    relation = field["relation"]
    parent = _instance_pk(instance)
    q = store.quote_identifier
    if field["kind"] == "membership_read":
        user = relation["user"]
        selected = ', '.join(
            f'u.{q(f["column"])} AS {q(f["name"])}' for f in relation["output_fields"])
        return [dict(row) for row in await store.access_sql(
            f'SELECT {selected} FROM {q(user["table"])} u '
            f'JOIN {q(relation["table"])} m ON m.{q(relation["user_column"])} = u.{q(user["pk"])} '
            f'WHERE m.{q(relation["parent_column"])} = @p0@', [parent])]
    ordering = ', '.join(q(term.lstrip('-')) + (' DESC' if term.startswith('-') else ' ASC')
                         for term in relation["ordering"])
    order_sql = ' ORDER BY ' + ordering if ordering else ''
    rows = await store.access_sql(
        f'SELECT {q(relation["column"])} AS value FROM {q(relation["db_table"])} '
        f'WHERE {q(relation["foreign_key"])} = @p0@ AND {q(relation["filter_column"])} = @p1@'
        + order_sql, [parent, relation["value"]])
    return [{relation["output"]:row["value"]} for row in rows[:relation["limit"]]]
"""


def delete_helpers(engine: str) -> str:
    return {
        "tortoise": """
async def access_delete(tables, value):
    q = quote_identifier
    async with in_transaction() as conn:
        sqlite = conn.capabilities.dialect == 'sqlite'
        mark = '?' if sqlite else '$1'
        for spec in tables:
            await conn.execute_query(
                f'DELETE FROM {q(spec["table"])} WHERE {q(spec["column"])} = {mark}',
                [_access_value(value, sqlite)])
""",
        "sqlalchemy": """
async def access_delete(tables, value):
    q = quote_identifier
    async with _sessionmaker()() as session:
        async with session.begin():
            for spec in tables:
                await session.execute(text(
                    f'DELETE FROM {q(spec["table"])} WHERE {q(spec["column"])} = :value'),
                    {'value':_access_value(value, database_url().startswith('sqlite'))})
""",
        "psycopg": """
async def access_delete(tables, value):
    q = quote_identifier
    async with await psycopg.AsyncConnection.connect(_CONNINFO) as conn:
        async with conn.cursor() as cur:
            for spec in tables:
                await cur.execute(
                    f'DELETE FROM {q(spec["table"])} WHERE {q(spec["column"])} = %s', [value])
""",
        "django": """
def _access_delete(tables, value):
    from django.db import connection, transaction
    q = quote_identifier
    with transaction.atomic(), connection.cursor() as cur:
        for spec in tables:
            cur.execute(f'DELETE FROM {q(spec["table"])} WHERE {q(spec["column"])} = %s',
                        [_access_value(value, connection.vendor == 'sqlite')])

async def access_delete(tables, value):
    await sync_to_async(_access_delete, thread_sensitive=True)(tables, value)
""",
    }[engine]

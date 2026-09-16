# SPDX-License-Identifier: Apache-2.0
"""Execute normalized membership contracts with synchronous SQLAlchemy."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

_RELATION_KEYS = {
    "db_table",
    "pk",
    "pk_kind",
    "object_name",
    "foreign_key",
    "field",
    "table",
    "parent_column",
    "user_column",
}


class ParentCreateMissing(RuntimeError):
    """The source parent lookup escapes serializer validation as a server error."""


def _membership_relation(relation: Any) -> None:
    if not isinstance(relation, dict) or set(relation) != _RELATION_KEYS:
        raise ValueError("unsupported membership relation")
    if relation["pk_kind"] not in {"integer", "big_integer", "uuid"}:
        raise ValueError("unsupported membership key kind")
    if relation["foreign_key"] is not None and not isinstance(relation["foreign_key"], str):
        raise ValueError("unsupported membership foreign key")
    if not all(isinstance(relation[name], str) for name in _RELATION_KEYS - {"foreign_key"}):
        raise ValueError("invalid membership relation")


def qualify_access(access: dict[str, Any], resource: dict[str, Any]) -> None:
    """Reject access or serializer facts outside the normalized supported subset."""

    allowed = {
        "query",
        "permission",
        "creator_membership",
        "delete_tables",
        "member_action",
        "serializer",
        "parameter_regexes",
    }
    if set(access) - allowed:
        raise ValueError("unsupported access contract")
    query = access.get("query")
    if query:
        if set(query) != {"version", "filters", "ordering"} or query["version"] != 1:
            raise ValueError("unsupported access query version")
        if not isinstance(query["ordering"], (list, tuple)) or not all(
            isinstance(term, str) for term in query["ordering"]
        ):
            raise ValueError("unsupported access query ordering")
        for rule in query["filters"]:
            if rule.get("kind") == "member" and set(rule) == {"kind", "relation"}:
                _membership_relation(rule["relation"])
            elif rule.get("kind") == "equal" and set(rule) == {"kind", "field", "value"}:
                value = rule["value"]
                if (
                    not isinstance(rule["field"], str)
                    or not isinstance(value, dict)
                    or value.get("kind") not in {"kwarg", "constant"}
                    or (value["kind"] == "kwarg" and set(value) != {"kind", "name"})
                    or (value["kind"] == "constant" and set(value) != {"kind", "value"})
                ):
                    raise ValueError("unsupported access equality filter")
            else:
                raise ValueError("unsupported access query filter")
    permission = access.get("permission")
    if permission:
        if (
            set(permission) != {"kind", "relation", "parent_param", "superuser"}
            or permission["kind"] != "membership"
            or permission["superuser"] is not True
            or (
                permission["parent_param"] is not None
                and not isinstance(permission["parent_param"], str)
            )
        ):
            raise ValueError("unsupported access permission")
        _membership_relation(permission["relation"])
    creator = access.get("creator_membership")
    if creator:
        _membership_relation(creator)
    for table in access.get("delete_tables", ()):
        if set(table) != {"table", "column"} or not all(
            isinstance(table[name], str) for name in ("table", "column")
        ):
            raise ValueError("unsupported access deletion")
    patterns = access.get("parameter_regexes", {})
    if not isinstance(patterns, dict) or not all(
        isinstance(name, str) and isinstance(pattern, str) for name, pattern in patterns.items()
    ):
        raise ValueError("unsupported access parameter pattern")
    action = access.get("member_action")
    if action:
        expected = {
            "action",
            "relation",
            "field",
            "touches",
            "required",
            "allow_empty",
            "messages",
            "child_messages",
            "user",
            "param",
        }
        if (
            set(action) != expected
            or action["action"] not in {"add", "remove"}
            or action["required"] is not True
            or action["allow_empty"] is not False
            or not all(isinstance(item, str) for item in action["touches"])
            or not isinstance(access.get("serializer"), str)
        ):
            raise ValueError("unsupported member action")
        _membership_relation(action["relation"])
        if set(action["user"]) != {"table", "pk", "active", "superuser"}:
            raise ValueError("unsupported member user contract")
    for field in resource.get("fields", ()):
        kind = field.get("kind")
        if kind == "membership_read":
            relation = field.get("relation") or {}
            base = {key: relation[key] for key in _RELATION_KEYS if key in relation}
            _membership_relation(base)
            if set(relation) != _RELATION_KEYS | {"user", "output_fields"}:
                raise ValueError("unsupported membership representation")
        elif kind == "related_preview":
            relation = field.get("relation") or {}
            if set(relation) != {
                "db_table",
                "pk",
                "pk_kind",
                "object_name",
                "foreign_key",
                "column",
                "filter_column",
                "value",
                "limit",
                "output",
                "ordering",
            }:
                raise ValueError("unsupported related preview")
    create = resource.get("create_contract")
    if (
        create
        and create.get("style") == "parent_duplicate"
        and set(create)
        != {
            "style",
            "parent",
            "param",
            "column",
            "name",
            "flag",
            "message",
        }
    ):
        raise ValueError("unsupported parent duplicate contract")


def _table(tables: Mapping[str, Any], name: str) -> Any:
    try:
        return tables[name]
    except KeyError as error:
        raise ValueError(f"access contract references unknown table: {name}") from error


def _coerce(column: Any, value: Any) -> Any:
    expected = column.type.python_type
    if isinstance(value, expected):
        return value
    return expected(value)


def _user_value(user: Mapping[str, Any] | None, auth: dict[str, Any], name: str) -> Any:
    return None if user is None else user[auth["user"][name]]


def scope_statement(
    statement: Any,
    table: Any,
    tables: Mapping[str, Any],
    access: dict[str, Any],
    kwargs: Mapping[str, Any],
    user_id: Any,
) -> Any:
    """Apply a captured ``get_queryset`` contract as bound SQL."""

    import sqlalchemy as sa

    query = access.get("query") or {}
    if not query:
        return statement
    if query.get("version") != 1:
        raise ValueError("unsupported access query version")
    for rule in query.get("filters", ()):
        kind = rule.get("kind")
        if kind == "member":
            relation = rule["relation"]
            membership = _table(tables, relation["table"])
            parent = table.c[relation.get("foreign_key") or relation["pk"]]
            condition = sa.exists(
                sa.select(1).where(
                    membership.c[relation["parent_column"]] == parent,
                    membership.c[relation["user_column"]] == user_id,
                )
            )
            statement = statement.where(condition if user_id is not None else sa.false())
        elif kind == "equal":
            column = table.c[rule["field"]]
            value = rule["value"]
            if value.get("kind") == "kwarg":
                raw = kwargs.get(value["name"])
            elif value.get("kind") == "constant":
                raw = value.get("value")
            else:
                raise ValueError("unsupported access equality value")
            try:
                expected = _coerce(column, raw)
            except (TypeError, ValueError, AttributeError):
                statement = statement.where(sa.false())
            else:
                statement = statement.where(column == expected)
        else:
            raise ValueError("unsupported access query filter")
    for term in query.get("ordering", ()):
        column = table.c[str(term).lstrip("-")]
        statement = statement.order_by(column.desc() if str(term).startswith("-") else column)
    return statement


def permission_error(
    session: Any,
    tables: Mapping[str, Any],
    access: dict[str, Any],
    instance: Mapping[str, Any] | None,
    kwargs: Mapping[str, Any],
    user: Mapping[str, Any] | None,
    auth: dict[str, Any],
) -> tuple[dict[str, str], int] | None:
    """Return the captured DRF membership denial, preserving parent lookup order."""

    import sqlalchemy as sa

    permission = access.get("permission")
    if not permission:
        return None
    if permission.get("kind") != "membership" or permission.get("superuser") is not True:
        raise ValueError("unsupported access permission")
    if user is not None and bool(_user_value(user, auth, "superuser")):
        return None
    relation = permission["relation"]
    parent_param = permission.get("parent_param")
    if parent_param:
        parent_table = _table(tables, relation["db_table"])
        column = parent_table.c[relation["pk"]]
        try:
            parent = _coerce(column, kwargs.get(parent_param))
        except (TypeError, ValueError, AttributeError):
            parent = None
        exists = (
            parent is not None
            and session.execute(sa.select(column).where(column == parent)).first() is not None
        )
        if not exists:
            return {"detail": f"No {relation['object_name']} matches the given query."}, 404
    else:
        if instance is None:
            raise ValueError("object membership permission requires an instance")
        parent = instance[relation.get("foreign_key") or relation["pk"]]
    if user is not None:
        membership = _table(tables, relation["table"])
        member = session.execute(
            sa.select(sa.literal(True)).where(
                sa.exists(
                    sa.select(1).where(
                        membership.c[relation["parent_column"]] == parent,
                        membership.c[relation["user_column"]] == _user_value(user, auth, "pk"),
                    )
                )
            )
        ).scalar_one_or_none()
        if member:
            return None
    message = auth["messages"]["no_credentials" if user is None else "forbidden"]
    return {"detail": message}, 401 if user is None else 403


def serialize_field(
    session: Any,
    tables: Mapping[str, Any],
    field: dict[str, Any],
    parent_key: Any,
) -> list[dict[str, Any]]:
    """Serialize one captured M2M membership or bounded related preview field."""

    import sqlalchemy as sa

    relation = field["relation"]
    if field.get("kind") == "membership_read":
        user = relation["user"]
        users = _table(tables, user["table"])
        membership = _table(tables, relation["table"])
        selected = [
            users.c[item["column"]].label(item["name"]) for item in relation["output_fields"]
        ]
        statement = (
            sa.select(*selected)
            .select_from(
                users.join(
                    membership,
                    membership.c[relation["user_column"]] == users.c[user["pk"]],
                )
            )
            .where(membership.c[relation["parent_column"]] == parent_key)
        )
    elif field.get("kind") == "related_preview":
        related = _table(tables, relation["db_table"])
        statement = sa.select(related.c[relation["column"]].label(relation["output"])).where(
            related.c[relation["foreign_key"]] == parent_key,
            related.c[relation["filter_column"]] == relation["value"],
        )
        for term in relation.get("ordering", ()):
            column = related.c[str(term).lstrip("-")]
            statement = statement.order_by(column.desc() if str(term).startswith("-") else column)
        statement = statement.limit(int(relation["limit"]))
    else:
        raise ValueError("unsupported access representation")
    return [dict(row) for row in session.execute(statement).mappings()]


def prepare_parent_create(
    session: Any,
    tables: Mapping[str, Any],
    resource: dict[str, Any],
    contract: dict[str, Any],
    kwargs: Mapping[str, Any],
    values: dict[str, Any],
) -> tuple[dict[str, Any], tuple[Any, int] | None]:
    """Inject a URL parent and reject the captured unfinished-name duplicate."""

    import sqlalchemy as sa

    if contract.get("style") != "parent_duplicate":
        raise ValueError("unsupported parent create contract")
    child = _table(tables, resource["db_table"])
    parent_spec = contract["parent"]
    parent_table = _table(tables, parent_spec["db_table"])
    parent_column = parent_table.c[parent_spec["pk"]]
    try:
        parent = _coerce(parent_column, kwargs.get(contract["param"]))
    except (TypeError, ValueError, AttributeError):
        parent = None
    if (
        parent is None
        or session.execute(sa.select(parent_column).where(parent_column == parent)).first() is None
    ):
        raise ParentCreateMissing(parent_spec["object_name"])
    duplicate = session.execute(
        sa.select(sa.literal(True)).where(
            sa.exists(
                sa.select(1).where(
                    child.c[contract["column"]] == parent,
                    child.c[contract["name"]] == values[contract["name"]],
                    child.c[contract["flag"]].is_(False),
                )
            )
        )
    ).scalar_one_or_none()
    if duplicate:
        return {}, ([contract["message"]], 400)
    return {**values, contract["column"]: parent}, None


def add_creator_membership(
    session: Any,
    tables: Mapping[str, Any],
    access: dict[str, Any],
    parent_key: Any,
    user_id: Any,
) -> None:
    """Apply a captured ``instance.members.add(request.user)`` after create."""

    import sqlalchemy as sa

    relation = access.get("creator_membership")
    if not relation:
        return
    if user_id is None:
        raise ValueError("creator membership requires an authenticated user")
    membership = _table(tables, relation["table"])
    exists = session.execute(
        sa.select(sa.literal(True)).where(
            sa.exists(
                sa.select(1).where(
                    membership.c[relation["parent_column"]] == parent_key,
                    membership.c[relation["user_column"]] == user_id,
                )
            )
        )
    ).scalar_one_or_none()
    if not exists:
        session.execute(
            sa.insert(membership).values(
                {
                    relation["parent_column"]: parent_key,
                    relation["user_column"]: user_id,
                }
            )
        )


def _action_error(field: str, message: str) -> tuple[dict[str, list[str]], int]:
    return {field: [message]}, 400


def member_action(
    session: Any,
    tables: Mapping[str, Any],
    resource: dict[str, Any],
    access: dict[str, Any],
    instance: Mapping[str, Any],
    data: Any,
) -> tuple[dict[str, Any], int]:
    """Validate and execute one captured add/remove member action."""

    import sqlalchemy as sa

    contract = access.get("member_action")
    if not contract or contract.get("action") not in {"add", "remove"}:
        raise ValueError("unsupported member action")
    name = contract["field"]
    messages = contract["messages"]
    if not isinstance(data, dict):
        return {
            "non_field_errors": [
                f"Invalid data. Expected a dictionary, but got {type(data).__name__}."
            ]
        }, 400
    if name not in data:
        if contract["required"]:
            return _action_error(name, messages["required"])
        raise ValueError("member action omitted its captured field")
    raw = data[name]
    if raw is None:
        return _action_error(name, messages["null"])
    if not isinstance(raw, list):
        return _action_error(name, messages["not_a_list"].format(input_type=type(raw).__name__))
    if not raw and not contract["allow_empty"]:
        return _action_error(name, messages["empty"])

    user = contract["user"]
    users = _table(tables, user["table"])
    user_column = users.c[user["pk"]]
    validated = []
    for value in raw:
        try:
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise TypeError
            key = _coerce(user_column, int(value))
        except (TypeError, ValueError, OverflowError, AttributeError):
            return _action_error(
                name,
                contract["child_messages"]["incorrect_type"].format(data_type=type(value).__name__),
            )
        if session.execute(sa.select(user_column).where(user_column == key)).first() is None:
            return _action_error(
                name,
                contract["child_messages"]["does_not_exist"].format(pk_value=value),
            )
        validated.append(key)

    relation = contract["relation"]
    membership = _table(tables, relation["table"])
    parent = instance[resource["pk_column"]]
    for key in validated:
        condition = sa.and_(
            membership.c[relation["parent_column"]] == parent,
            membership.c[relation["user_column"]] == key,
        )
        if contract["action"] == "remove":
            session.execute(sa.delete(membership).where(condition))
        elif (
            session.execute(
                sa.select(sa.literal(True)).where(sa.exists(sa.select(1).where(condition)))
            ).scalar_one_or_none()
            is None
        ):
            session.execute(
                sa.insert(membership).values(
                    {relation["parent_column"]: parent, relation["user_column"]: key}
                )
            )

    table = _table(tables, resource["db_table"])
    updates = {}
    for column_name in contract.get("touches", ()):
        column = table.c[column_name]
        now = dt.datetime.now(dt.UTC)
        updates[column_name] = (
            now if getattr(column.type, "timezone", False) else now.replace(tzinfo=None)
        )
    if updates:
        session.execute(
            sa.update(table).where(table.c[resource["pk_column"]] == parent).values(**updates)
        )
    statement = (
        sa.select(user_column)
        .select_from(
            users.join(
                membership,
                membership.c[relation["user_column"]] == user_column,
            )
        )
        .where(membership.c[relation["parent_column"]] == parent)
    )
    return {name: list(session.execute(statement).scalars())}, 200

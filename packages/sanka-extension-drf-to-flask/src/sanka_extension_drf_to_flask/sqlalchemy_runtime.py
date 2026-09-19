# SPDX-License-Identifier: Apache-2.0
"""Copied unchanged into generated projects; synchronous request and SQL execution."""

from __future__ import annotations

import copy
import datetime as dt
import importlib
import json
import re
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

import sqlalchemy as sa
from flask import Blueprint, Flask, Response, request
from werkzeug.routing import BaseConverter

from .native_runtime import _ValidationError


class _AllPaths(BaseConverter):
    regex = ".*"
    part_isolating = False


def _response(payload: Any, status: int = 200) -> Response:
    return Response(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
        status=status,
        content_type="application/json",
    )


_DATETIME = re.compile(
    r"(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})[T ]"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{1,2})(?::(?P<second>\d{1,2})"
    r"(?:[.,](?P<microsecond>\d{1,6})\d{0,6})?)?\s*"
    r"(?P<zone>Z|[+-]\d{2}(?::?\d{2})?)?$"
)


def _parse_datetime(value: str) -> dt.datetime:
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        match = _DATETIME.fullmatch(value)
        if match is None:
            raise
        parts = match.groupdict()
        zone = parts.pop("zone")
        microseconds = parts.pop("microsecond")
        offset = None
        if zone == "Z":
            offset = dt.UTC
        elif zone:
            minutes = 60 * int(zone[1:3]) + (int(zone[-2:]) if len(zone) > 3 else 0)
            offset = dt.timezone(dt.timedelta(minutes=-minutes if zone[0] == "-" else minutes))
        numbers = {name: int(item) for name, item in parts.items() if item is not None}
        if microseconds:
            numbers["microsecond"] = int(microseconds.ljust(6, "0"))
        return dt.datetime(**numbers, tzinfo=offset)


def _clean(field: dict[str, Any], value: Any, scalar: Any) -> Any:
    messages = field["messages"]
    if value is None:
        if field["allow_null"]:
            return None
        raise _ValidationError([messages["null"]])
    kind = field["kind"]
    if kind == "boolean":
        value = value.lower() if isinstance(value, str) else value
        try:
            if value in {"t", "y", "yes", "true", "on", "1", 1}:
                return True
            if value in {"f", "n", "no", "false", "off", "0", 0}:
                return False
            if field["allow_null"] and value in {"null", ""}:
                return None
        except TypeError:
            pass
    elif kind == "uuid":
        try:
            if isinstance(value, int):
                return uuid.UUID(int=value)
            if isinstance(value, str):
                return uuid.UUID(hex=value)
        except ValueError:
            pass
    elif kind == "datetime":
        if isinstance(value, str):
            try:
                from zoneinfo import ZoneInfo

                parsed = _parse_datetime(value)
                if parsed.tzinfo is None and field["timezone"]:
                    parsed = parsed.replace(tzinfo=ZoneInfo(field["timezone"]))
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone(dt.UTC)
                    if not field["timezone"]:
                        parsed = parsed.replace(tzinfo=None)
                return parsed
            except OverflowError:
                raise _ValidationError([messages["overflow"]]) from None
            except ValueError:
                pass
    elif field["scalar_kind"]:
        # Reuse the same scalar validator as existing Flask conversions.
        return scalar.clean_field({**field, "kind": field["scalar_kind"]}, value)
    raise _ValidationError([messages["invalid"]])


def _serialize(resource: dict[str, Any], row: Any, session: Any, tables: Any) -> dict[str, Any]:
    result = {}
    for field in resource["fields"]:
        if field["write_only"]:
            continue
        if field["kind"] in {"membership_read", "related_preview"}:
            access_runtime = importlib.import_module(__package__ + ".sqlalchemy_access")
            result[field["name"]] = access_runtime.serialize_field(
                session, tables, field, row[resource["pk_column"]]
            )
            continue
        if field["kind"] == "nested_many":
            child = field["child"]
            child_table = tables[child["db_table"]]
            query = sa.select(child_table).where(
                child_table.c[field["foreign_key"]] == row[resource["pk_column"]]
            )
            for term in child["ordering"]:
                column = child_table.c[term.lstrip("-")]
                query = query.order_by(column.desc() if term.startswith("-") else column.asc())
            result[field["name"]] = [
                _serialize(child, item, session, tables)
                for item in session.execute(query).mappings()
            ]
            continue
        value = row[field["column"]]
        if value is not None:
            if field["kind"] in {"uuid", "related_uuid"}:
                value = str(value)
            elif field["kind"] == "decimal":
                value = str(Decimal(value).quantize(Decimal(1).scaleb(-field["decimal_places"])))
            elif field["kind"] == "datetime":
                if field["timezone"]:
                    from zoneinfo import ZoneInfo

                    if value.tzinfo is None:
                        value = value.replace(tzinfo=dt.UTC)
                    value = value.astimezone(ZoneInfo(field["timezone"]))
                elif value.tzinfo is not None:
                    from zoneinfo import ZoneInfo

                    storage_timezone = ZoneInfo(field.get("storage_timezone", "UTC"))
                    value = value.astimezone(storage_timezone).replace(tzinfo=None)
                value = value.isoformat().replace("+00:00", "Z")
            elif field["kind"] == "big_integer" and field["coerce_to_string"]:
                value = str(value)
        result[field["name"]] = value
    return result


def _validate(
    resource: dict[str, Any],
    data: Any,
    partial: bool,
    session: Any,
    table: Any,
    instance: Any,
    scalar: Any,
    tables: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(data, dict):
        return {}, {
            "non_field_errors": [
                "Invalid data. Expected a dictionary, but got " + type(data).__name__ + "."
            ]
        }
    values: dict[str, Any] = {}
    errors: dict[str, Any] = {}
    for field in resource["fields"]:
        if field["read_only"]:
            continue
        name = field["name"]
        if name not in data:
            if partial:
                continue
            if field["has_default"]:
                values[field["column"]] = field["default"]
            elif field["required"]:
                errors[name] = [field["messages"]["required"]]
            continue
        try:
            if field["kind"] == "nested_many":
                raw = data[name]
                if raw is None:
                    raise _ValidationError([field["messages"]["null"]])
                if not isinstance(raw, list):
                    raise _ValidationError(
                        {
                            "non_field_errors": [
                                field["messages"]["not_a_list"].format(
                                    input_type=type(raw).__name__
                                )
                            ]
                        }
                    )
                child = field["child"]
                children, child_errors = [], []
                for item in raw:
                    cleaned, error = _validate(
                        child,
                        item,
                        partial,
                        session,
                        tables[child["db_table"]],
                        None,
                        scalar,
                        tables,
                    )
                    if item is None:
                        error = field["child_null_error"]
                    children.append(cleaned)
                    child_errors.append(error)
                if any(child_errors):
                    raise _ValidationError(
                        {str(i): error for i, error in enumerate(child_errors) if error}
                        if field["indexed_errors"]
                        else child_errors
                    )
                value = children
            else:
                value = _clean(field, data[name], scalar)
            if field["unique"] and value is not None:
                query = sa.select(table.c[resource["pk_column"]]).where(
                    table.c[field["column"]] == value
                )
                if instance is not None:
                    query = query.where(
                        table.c[resource["pk_column"]] != instance[resource["pk_column"]]
                    )
                if session.execute(query.limit(1)).first() is not None:
                    raise _ValidationError([field["unique_message"]])
            values[field["column"]] = value
        except _ValidationError as error:
            errors[name] = error.detail
    return values, errors


def _authenticate(auth: dict[str, Any], tables: Any, sessions: Any) -> tuple[Any, Response | None]:
    if auth.get("kind") == "session":
        from flask import current_app

        session_runtime = importlib.import_module(__package__ + ".sqlalchemy_sessions")
        return cast(
            tuple[Any, Response | None],
            session_runtime.authenticate(current_app.extensions.get("sanka_session_runtime"), auth),
        )
    if not auth.get("token_keyword"):
        return None, None
    messages = auth["messages"]

    def failure(name: str) -> tuple[None, Response]:
        response = _response({"detail": messages[name]}, 401)
        response.headers["WWW-Authenticate"] = messages["www_authenticate"]
        return None, response

    try:
        parts = request.headers.get("Authorization", "").encode("latin-1").split()
    except UnicodeError:
        return failure("invalid_characters")
    if not parts or parts[0].lower() != auth["token_keyword"].encode().lower():
        return failure("no_credentials") if auth.get("require_authenticated") else (None, None)
    if len(parts) == 1:
        return failure("empty_header")
    if len(parts) > 2:
        return failure("spaced_header")
    try:
        key = parts[1].decode("utf-8")
    except UnicodeError:
        return failure("invalid_characters")
    tokens = tables[auth["token_db_table"]]
    user = auth["user"]
    users = tables[user["table"]]
    query = (
        sa.select(users)
        .join(tokens, users.c[user["pk"]] == tokens.c[auth["token_user_column"]])
        .where(tokens.c[auth["token_key_column"]] == key)
    )
    with sessions() as session:
        row = session.execute(query).mappings().first()
    if row is None:
        return failure("invalid_token")
    if not row[user["active"]]:
        return failure("inactive_user")
    return row, None


def create_app(config: dict[str, Any] | None = None) -> Flask:
    """Create an independent app and engine; never create tables or run migrations."""
    contract = json.loads(
        (Path(__file__).resolve().parents[1] / "native_contract.json").read_text()
    )
    database = importlib.import_module(contract["module_prefix"] + "database")
    tables = importlib.import_module(contract["module_prefix"] + "models").TABLES
    scalar = importlib.import_module(__package__ + ".scalars").ScalarInput()
    listing_runtime = importlib.import_module(__package__ + ".sqlalchemy_listing")
    access_runtime: Any = (
        importlib.import_module(__package__ + ".sqlalchemy_access")
        if any(view["access"] for view in contract["views"].values())
        else None
    )
    deletion = (
        importlib.import_module(__package__ + ".sqlalchemy_deletion")
        if contract["collected_delete"]
        else None
    )
    app = Flask(__name__)
    if contract["request_body_limit"] is not None:
        app.config.update(MAX_CONTENT_LENGTH=contract["request_body_limit"])
    app.config.update(config or {})
    app.url_map.merge_slashes = False
    app.url_map.converters["allpaths"] = _AllPaths
    engine = database.make_engine(app.config.get("DATABASE_URL"))
    app.extensions["sanka_engine"] = engine
    sessions = database.sessions(engine)
    app.extensions["sanka_sessions"] = sessions
    app.extensions["sanka_tables"] = tables
    blueprint = Blueprint("source_api", __name__)
    routes: dict[tuple[str, str], dict[str, Any]] = {}
    for route in contract["routes"]:
        routes.setdefault((route["path"], route["view"]), {})[route["method"]] = route
    patterns = [(re.compile(p["regex"]), p) for p in contract["patterns"]]
    roots = {root["path"]: root["links"] for root in contract["api_roots"]}
    messages = contract["generic_messages"]

    def server_error() -> Response:
        return Response(
            contract["server_error_response"]["body"],
            status=500,
            content_type=contract["server_error_response"]["content_type"],
        )

    def not_found() -> Response:
        return _response({"detail": messages.get("not_found", "Not found.")}, 404)

    def detail_row(
        resource: Any, session: Any, table: Any, kwargs: Any, view: Any, user_id: Any
    ) -> tuple[Any, dict[str, list[str]] | None, bool]:
        parameter = kwargs.get(view.get("lookup_url_kwarg") or resource["lookup"])
        column = table.c[resource["lookup_column"]]
        statement = sa.select(table)
        if access_runtime is not None:
            statement = access_runtime.scope_statement(
                statement, table, tables, view["access"], kwargs, user_id
            )
        statement, error = (
            listing_runtime.apply_filters(
                statement, table, resource, view["listing"], dict(request.args.lists())
            )
            if request.method != "OPTIONS"
            else (statement, None)
        )
        if error:
            return None, error, False
        try:
            value = (
                parameter
                if isinstance(parameter, column.type.python_type)
                else column.type.python_type(parameter)
            )
        except (TypeError, ValueError, AttributeError):
            return None, None, True
        statement = statement.where(column == value)
        return session.execute(statement).mappings().first(), None, False

    def handle(methods: dict[str, Any], kwargs: dict[str, Any]) -> Response:
        order = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"]
        allowed = [
            m for m in order if m in methods or m == "OPTIONS" or (m == "HEAD" and "GET" in methods)
        ]
        method = "GET" if request.method == "HEAD" else request.method
        route = methods.get(method) or next(iter(methods.values()))

        def finish(response: Response) -> Response:
            response.headers["Allow"] = ", ".join(allowed)
            if response.status_code == 401 and auth.get("messages", {}).get("www_authenticate"):
                response.headers["WWW-Authenticate"] = auth["messages"]["www_authenticate"]
            return response

        auth = contract["views"].get(route["view"], {}).get("auth") or {}
        suffix = "format" in kwargs
        format_param = contract["format_query_param"]
        format_name = (
            kwargs.pop("format").strip("./")
            if suffix
            else request.args.get(format_param)
            if format_param
            else None
        )
        if format_name and format_name != "json":
            return finish(not_found())
        if not request.accept_mimetypes.accept_json and request.headers.get("Accept"):
            return finish(
                _response({"detail": "Could not satisfy the request Accept header."}, 406)
            )
        user, failure = _authenticate(auth, tables, sessions)
        if failure is not None:
            return finish(failure)
        view = contract["views"].get(route["view"], {})
        access = view.get("access") or {}
        user_id = user[auth["user"]["pk"]] if user is not None else None
        if access.get("permission", {}).get("parent_param"):
            with sessions() as session:
                denied = access_runtime.permission_error(
                    session, tables, access, None, kwargs, user, auth
                )
                if denied:
                    return finish(_response(*denied))
        if method not in methods and method != "OPTIONS":
            response = _response(
                {
                    "detail": messages.get(
                        "method_not_allowed", 'Method "{method}" not allowed.'
                    ).format(method=request.method)
                },
                405,
            )
        elif route["serializer"] is None:
            if method == "OPTIONS":
                response = _response(copy.deepcopy(route["options"]["anonymous"]))
            else:
                links = roots.get(route["path"].replace("<drf_format_suffix:format>", ""), [])
                response = _response(
                    {
                        name: request.url_root.rstrip("/")
                        + (path.rstrip("/") + ".json" if suffix else path)
                        + (
                            "?" + urlencode({format_param: request.args.get(format_param)})
                            if format_param and format_param in request.args
                            else ""
                        )
                        for name, path in links
                    }
                )
        else:
            resource = contract["resources"][route["serializer"]]
            table = tables[resource["db_table"]]
            with sessions() as session:
                detail = route["operation"] in {
                    "retrieve",
                    "update",
                    "partial_update",
                    "destroy",
                } or bool(access.get("member_action"))
                instance, filter_error, invalid_lookup = (
                    detail_row(resource, session, table, kwargs, view, user_id)
                    if detail
                    else (None, None, False)
                )
                denied = (
                    access_runtime.permission_error(
                        session, tables, access, instance, kwargs, user, auth
                    )
                    if instance is not None and access.get("permission")
                    else None
                )
                foreign = bool(
                    auth.get("owner_attname")
                    and instance is not None
                    and instance[auth["owner_attname"]] != user[auth["user"]["pk"]]
                )
                if filter_error:
                    response = _response(filter_error, 400)
                elif method == "OPTIONS":
                    metadata = copy.deepcopy(
                        route["options"]["authorized" if user is not None else "anonymous"]
                    )
                    if detail and (instance is None or foreign or denied):
                        metadata.get("actions", {}).pop("PUT", None)
                        if not metadata.get("actions"):
                            metadata.pop("actions", None)
                    response = _response(metadata)
                elif detail and invalid_lookup:
                    response = not_found()
                elif detail and instance is None:
                    response = _response({"detail": resource["not_found"]}, 404)
                elif denied and not access.get("member_action"):
                    response = _response(*denied)
                elif foreign and request.method not in {"GET", "HEAD", "OPTIONS"}:
                    response = _response({"detail": auth["messages"]["forbidden"]}, 403)
                elif route["operation"] == "list":
                    payload, status = listing_runtime.list_records(
                        session,
                        table,
                        resource,
                        contract["views"][route["view"]]["listing"],
                        dict(request.args.lists()),
                        request.url,
                        lambda row: _serialize(resource, row, session, tables),
                        base_statement=(
                            access_runtime.scope_statement(
                                sa.select(table), table, tables, access, kwargs, user_id
                            )
                            if access_runtime
                            else None
                        ),
                    )
                    response = _response(payload, status)
                elif route["operation"] == "retrieve":
                    response = _response(_serialize(resource, instance, session, tables))
                elif route["operation"] == "destroy":
                    try:
                        if deletion is not None:
                            deletion.delete_instance(
                                session, tables, contract["schema"], resource["db_table"], instance
                            )
                        else:
                            assert instance is not None
                            session.execute(
                                sa.delete(table).where(
                                    table.c[resource["pk_column"]]
                                    == instance[resource["pk_column"]]
                                )
                            )
                        session.commit()
                        response = Response(status=204)
                        response.headers.pop("Content-Type", None)
                    except Exception as error:
                        if not isinstance(error, sa.exc.IntegrityError) and not (
                            deletion is not None and isinstance(error, deletion.DeletionBlocked)
                        ):
                            raise
                        session.rollback()
                        response = server_error()
                else:
                    content_type = request.mimetype
                    if request.content_length and content_type != "application/json":
                        response = _response(
                            {
                                "detail": (
                                    f'Unsupported media type "{request.content_type}" in request.'
                                )
                            },
                            415,
                        )
                    else:
                        try:
                            raw = request.get_data()

                            def reject_constant(value: str) -> Any:
                                raise ValueError(
                                    "Out of range float values are not JSON compliant: "
                                    + repr(value)
                                )

                            data = (
                                json.loads(
                                    raw.decode(request.mimetype_params.get("charset", "utf-8")),
                                    parse_constant=reject_constant,
                                )
                                if raw
                                else {}
                            )
                        except (ValueError, UnicodeError) as error:
                            response = _response(
                                {"detail": "JSON parse error - " + str(error)}, 400
                            )
                        else:
                            if access.get("member_action"):
                                if denied:
                                    return finish(_response(*denied))
                                payload, status = access_runtime.member_action(
                                    session, tables, resource, access, instance, data
                                )
                                if status < 400:
                                    session.commit()
                                return finish(_response(payload, status))
                            values, errors = _validate(
                                resource,
                                data,
                                method == "PATCH",
                                session,
                                table,
                                instance,
                                scalar,
                                tables,
                            )
                            if errors:
                                response = _response(errors, 400)
                            else:
                                try:
                                    if instance is None:
                                        if (resource.get("create_contract") or {}).get(
                                            "style"
                                        ) == "parent_duplicate":
                                            try:
                                                values, rejected = (
                                                    access_runtime.prepare_parent_create(
                                                        session,
                                                        tables,
                                                        resource,
                                                        resource["create_contract"],
                                                        kwargs,
                                                        values,
                                                    )
                                                )
                                            except access_runtime.ParentCreateMissing:
                                                session.rollback()
                                                return finish(server_error())
                                            if rejected:
                                                return finish(_response(*rejected))
                                        if auth.get("inject_owner_attname"):
                                            values[auth["inject_owner_attname"]] = user[
                                                auth["user"]["pk"]
                                            ]
                                        if (resource.get("create_contract") or {}).get(
                                            "style"
                                        ) == "nested":
                                            nested = importlib.import_module(
                                                __package__ + ".sqlalchemy_nested"
                                            )
                                            pk = nested.create_nested(
                                                session, tables, resource, values
                                            )
                                        else:
                                            inserted = session.execute(
                                                sa.insert(table).values(**values)
                                            )
                                            pk = inserted.inserted_primary_key[0]
                                        if access.get("creator_membership"):
                                            access_runtime.add_creator_membership(
                                                session, tables, access, pk, user_id
                                            )
                                    else:
                                        for dropped in resource["update_drops"] or ():
                                            column = next(
                                                f["column"]
                                                for f in resource["fields"]
                                                if f["name"] == dropped
                                            )
                                            values.pop(column, None)
                                        pk = instance[resource["pk_column"]]
                                        if values or any(
                                            c.get("auto_now") for c in resource["columns"]
                                        ):
                                            session.execute(
                                                sa.update(table)
                                                .where(table.c[resource["pk_column"]] == pk)
                                                .values(**values)
                                            )
                                    row = (
                                        session.execute(
                                            sa.select(table).where(
                                                table.c[resource["pk_column"]] == pk
                                            )
                                        )
                                        .mappings()
                                        .one()
                                    )
                                    payload = _serialize(resource, row, session, tables)
                                    session.commit()
                                    response = _response(payload, 201 if instance is None else 200)
                                except _ValidationError as error:
                                    session.rollback()
                                    response = _response(error.detail, 400)
                                except sa.exc.IntegrityError:
                                    session.rollback()
                                    _, errors = _validate(
                                        resource,
                                        data,
                                        method == "PATCH",
                                        session,
                                        table,
                                        instance,
                                        scalar,
                                        tables,
                                    )
                                    response = _response(errors, 400) if errors else server_error()
        conditional = contract["views"].get(route["view"], {}).get("conditional")
        if (
            conditional
            and route["operation"] in conditional["operations"]
            and method != "OPTIONS"
            and response.status_code == 200
        ):
            carryover = importlib.import_module(__package__ + ".sqlalchemy_carryover")
            response = carryover.conditional_response(
                response, request.headers.get("If-None-Match", "")
            )
        overrides = view.get("response_overrides") or {}
        if (
            route["operation"] in overrides
            and method in methods
            and method != "OPTIONS"
            and 200 <= response.status_code < 300
        ):
            carryover = importlib.import_module(__package__ + ".sqlalchemy_carryover")
            response = carryover.override_response(response, overrides[route["operation"]])
        return finish(response)

    def dispatch(_path: str = "") -> Response:
        for regex, pattern in patterns:
            match = regex.fullmatch(_path)
            if match is None:
                continue
            kwargs = match.groupdict()
            try:
                for key, kind in pattern["converters"].items():
                    if kind == "int":
                        kwargs[key] = int(kwargs[key])
                    elif kind == "uuid":
                        kwargs[key] = uuid.UUID(kwargs[key])
            except ValueError:
                continue
            methods = routes.get((pattern["path"], pattern["view"]))
            if methods:
                return handle(methods, kwargs)
        return Response(
            contract["not_found_response"]["body"],
            status=404,
            content_type=contract["not_found_response"]["content_type"],
        )

    blueprint.add_url_rule(
        "/<allpaths:_path>",
        "dispatch",
        dispatch,
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"],
        provide_automatic_options=False,
        strict_slashes=False,
    )
    app.register_blueprint(blueprint)

    @app.after_request
    def source_headers(response: Response) -> Response:
        if not contract["http_security"].get("content_length"):
            response.automatically_set_content_length = False
            response.headers.pop("Content-Length", None)
        return response

    middleware = importlib.import_module(__package__ + ".sqlalchemy_middleware")
    middleware.install_middleware(app, contract, patterns)
    return app

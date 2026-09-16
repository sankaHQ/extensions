# SPDX-License-Identifier: Apache-2.0
"""Qualified DRF list semantics for generated synchronous SQLAlchemy targets."""

from __future__ import annotations

import base64
import copy
import importlib
import importlib.metadata
import inspect
import math
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from urllib import parse as urlparse
from uuid import UUID

_SMART_SPLIT = re.compile(
    r"""((?:[^\s'"]*(?:(?:"(?:[^"\\]|\\.)*" | '(?:[^'\\]|\\.)*')[^\s'"]*)+) | \S+)""",
    re.VERBOSE,
)
_CURSOR = re.compile(r"[A-Za-z0-9+/]+={0,2}\Z")
_FILTER_LOOKUPS = {"exact", "iexact", "in", "gt", "gte", "lt", "lte", "isnull"}
_SEARCH_LOOKUPS = {"icontains", "istartswith", "iexact"}


def _defines_methods(derived: type[Any], base: type[Any]) -> bool:
    for klass in derived.__mro__:
        if klass in {base, object}:
            break
        if any(inspect.isfunction(member) for member in vars(klass).values()):
            return True
    return False


def capture_stock_pagination(view_class: type[Any]) -> dict[str, Any] | None:
    """Capture configurable stock page/limit pagination without importing DRF at module load."""

    pagination = importlib.import_module("rest_framework.pagination")
    paginator_class = getattr(view_class, "pagination_class", None)
    if paginator_class is None:
        return None
    if inspect.isclass(paginator_class) and issubclass(
        paginator_class, pagination.CursorPagination
    ):
        return None  # The shared scanner owns the already-qualified cursor contract.
    for base, kind in (
        (pagination.PageNumberPagination, "page"),
        (pagination.LimitOffsetPagination, "limit-offset"),
    ):
        if not (inspect.isclass(paginator_class) and issubclass(paginator_class, base)):
            continue
        if _defines_methods(paginator_class, base):
            raise ValueError("custom pagination methods require manual adaptation")
        paginator = cast(Any, paginator_class)()
        if kind == "page":
            return {
                "kind": kind,
                "page_size": paginator.page_size,
                "page_param": str(paginator.page_query_param),
                "page_size_param": paginator.page_size_query_param,
                "max_page_size": paginator.max_page_size,
                "last_page_strings": [str(item) for item in paginator.last_page_strings],
                "invalid_page_message": "Invalid page.",
            }
        return {
            "kind": kind,
            "default_limit": paginator.default_limit,
            "limit_param": str(paginator.limit_query_param),
            "offset_param": str(paginator.offset_query_param),
            "max_limit": paginator.max_limit,
        }
    raise ValueError("pagination class requires manual adaptation")


def _serializer_fields(view_class: type[Any]) -> tuple[dict[str, Any], set[str], str]:
    serializers = importlib.import_module("rest_framework.serializers")
    serializer_class = getattr(view_class, "serializer_class", None)
    if not inspect.isclass(serializer_class):
        raise ValueError("listing requires a declared serializer_class")
    try:
        fields = dict(cast(Any, serializer_class)().fields)
    except Exception as error:
        raise ValueError("serializer fields require manual listing adaptation") from error
    names = {
        name
        for name, field in fields.items()
        if not getattr(field, "write_only", False) and getattr(field, "source", None) != "*"
    }
    text = {
        name
        for name, field in fields.items()
        if name in names and isinstance(field, (serializers.CharField, serializers.ChoiceField))
    }
    model = getattr(getattr(serializer_class, "Meta", None), "model", None)
    model_pk = getattr(getattr(model, "_meta", None), "pk", None)
    candidates = {
        "id",
        str(getattr(model_pk, "name", "id")),
        str(getattr(model_pk, "attname", "id")),
    }
    pk = next(
        (
            name
            for name, field in fields.items()
            if name in candidates or str(getattr(field, "source", "")) in candidates
        ),
        "id",
    )
    return fields, text, pk


def _capture_backends(view_class: type[Any]) -> dict[str, Any]:
    filters_module = importlib.import_module("rest_framework.filters")
    fields, text_fields, pk = _serializer_fields(view_class)
    field_names = set(fields)
    listing: dict[str, Any] = {}
    for backend in getattr(view_class, "filter_backends", ()):
        if inspect.isclass(backend) and issubclass(backend, filters_module.SearchFilter):
            if _defines_methods(backend, filters_module.SearchFilter):
                raise ValueError("custom search backend requires manual adaptation")
            specs = []
            for raw in getattr(view_class, "search_fields", ()) or ():
                raw = str(raw)
                prefix = raw[:1]
                if prefix in {"@", "$"}:
                    raise ValueError("search lookup requires manual adaptation")
                lookup = {"^": "istartswith", "=": "iexact"}.get(prefix, "icontains")
                name = raw[1:] if prefix in {"^", "="} else raw
                if "__" in name or name not in text_fields:
                    raise ValueError("search field requires manual adaptation")
                specs.append({"name": name, "lookup": lookup})
            backend_type = cast(Any, backend)
            listing["search"] = {"param": str(backend_type.search_param), "fields": specs}
        elif inspect.isclass(backend) and issubclass(backend, filters_module.OrderingFilter):
            if _defines_methods(backend, filters_module.OrderingFilter):
                raise ValueError("custom ordering backend requires manual adaptation")
            declared = getattr(view_class, "ordering_fields", None)
            if declared == "__all__":
                raise ValueError("unbounded ordering fields require manual adaptation")
            names = (
                [str(item) if isinstance(item, str) else str(item[0]) for item in declared]
                if declared
                else sorted(field_names)
            )
            default = getattr(view_class, "ordering", None)
            defaults = (
                [str(default)]
                if isinstance(default, str)
                else [str(item) for item in default or ()]
            )
            allowed = field_names | {"pk", pk}
            if any(name not in allowed for name in names) or any(
                term.lstrip("-") not in allowed for term in defaults
            ):
                raise ValueError("ordering field requires manual adaptation")
            backend_type = cast(Any, backend)
            listing["ordering"] = {
                "param": str(backend_type.ordering_param),
                "fields": names,
                "default": defaults or None,
                "rule": "drf",
                "pk": pk,
            }
        else:
            django_filters = importlib.import_module("django_filters.filters")
            django_backend = importlib.import_module(
                "django_filters.rest_framework.backends"
            ).DjangoFilterBackend
            if backend is not django_backend:
                raise ValueError("filter backend requires manual adaptation")
            if getattr(view_class, "filterset_class", None) is not None:
                raise ValueError("custom filtersets require manual adaptation")
            declared = getattr(view_class, "filterset_fields", None)
            if not declared:
                raise ValueError("django-filter requires explicit filterset_fields")
            pairs = (
                [(str(name), "exact") for name in declared]
                if not isinstance(declared, dict)
                else [
                    (str(name), str(lookup))
                    for name, lookups in declared.items()
                    for lookup in lookups
                ]
            )
            model = getattr(getattr(view_class, "serializer_class", None), "Meta", None)
            model = getattr(model, "model", None)
            queryset = getattr(view_class, "queryset", None)
            if model is None or queryset is None or getattr(queryset, "model", None) is not model:
                raise ValueError("django-filter model requires manual adaptation")
            model = cast(Any, model)
            filterset = django_backend().get_filterset_class(view_class(), queryset)
            if filterset is None:
                raise ValueError("django-filter requires explicit filterset_fields")
            specs = []
            for name, lookup in pairs:
                if name not in field_names or lookup not in _FILTER_LOOKUPS:
                    raise ValueError("django-filter field or lookup requires manual adaptation")
                if getattr(fields[name], "source", name) != name:
                    raise ValueError("django-filter serializer alias requires manual adaptation")
                param = name if lookup == "exact" else f"{name}__{lookup}"
                actual = filterset.base_filters.get(param)
                if actual is None or actual.lookup_expr != lookup:
                    raise ValueError("django-filter field requires manual adaptation")
                model_field = model._meta.get_field(name)
                boolean_filter = importlib.import_module(
                    "django_filters.rest_framework.filters"
                ).BooleanFilter
                if lookup == "isnull" or isinstance(actual, boolean_filter):
                    value_kind = "boolean"
                elif isinstance(actual, django_filters.NumberFilter):
                    value_kind = "number"
                elif isinstance(actual, django_filters.ChoiceFilter):
                    value_kind = "choice"
                elif isinstance(actual, django_filters.CharFilter):
                    value_kind = "string"
                else:
                    raise ValueError("django-filter field type requires manual adaptation")
                if value_kind == "choice" and any(
                    not isinstance(value, str) for value, _label in model_field.choices
                ):
                    raise ValueError("django-filter choice type requires manual adaptation")
                if value_kind == "choice" and actual.field.null_label is not None:
                    raise ValueError("django-filter null choice requires manual adaptation")
                if lookup == "iexact" and value_kind not in {"string", "choice"}:
                    raise ValueError("django-filter lookup requires manual adaptation")
                if lookup in {"gt", "gte", "lt", "lte"} and value_kind == "boolean":
                    raise ValueError("django-filter lookup requires manual adaptation")
                spec: dict[str, Any] = {
                    "param": param,
                    "name": name,
                    "lookup": lookup,
                    "value_kind": value_kind,
                }
                if value_kind == "choice":
                    spec["choices"] = [
                        str(value) for value, _label in actual.field.choices if value != ""
                    ]
                    spec["invalid_choice"] = str(actual.field.error_messages["invalid_choice"])
                elif value_kind == "number":
                    if model_field.get_internal_type() in {
                        "AutoField",
                        "BigAutoField",
                        "SmallAutoField",
                        "IntegerField",
                        "BigIntegerField",
                        "SmallIntegerField",
                        "PositiveIntegerField",
                        "PositiveBigIntegerField",
                        "PositiveSmallIntegerField",
                    }:
                        spec["number_kind"] = "integer"
                        connection = importlib.import_module("django.db").connection
                        minimum, maximum = connection.ops.integer_field_range(
                            model_field.get_internal_type()
                        )
                        spec["integer_range"] = [minimum, maximum]
                    try:
                        actual.field.clean("not-a-number")
                    except Exception as error:
                        spec["invalid"] = str(getattr(error, "messages", [str(error)])[0])
                    validator = actual.get_max_validator()
                    if validator is not None:
                        spec["max_value"] = str(validator.limit_value)
                        try:
                            actual.field.clean(str(Decimal(str(validator.limit_value)) * 10))
                        except Exception as error:
                            spec["max_value_error"] = str(
                                getattr(error, "messages", [str(error)])[0]
                            )
                elif value_kind == "string":
                    try:
                        actual.field.clean("contains\x00null")
                    except Exception as error:
                        spec["null_character_error"] = str(
                            getattr(error, "messages", [str(error)])[0]
                        )
                specs.append(spec)
            listing["filters"] = specs
            listing["django_filter_version"] = importlib.metadata.version("django-filter")
    return listing


def _validate_captured_listing(listing: dict[str, Any]) -> dict[str, Any]:
    captured = copy.deepcopy(listing)
    if (search := captured.get("search")) and any(
        spec.get("lookup") not in _SEARCH_LOOKUPS for spec in search.get("fields", ())
    ):
        raise ValueError("captured search lookup requires manual adaptation")
    if (ordering := captured.get("ordering")) and ordering.get("rule") not in {
        "drf",
        "append-pk-follow-last",
        "append-pk-asc",
    }:
        raise ValueError("captured ordering rule requires manual adaptation")
    if any(spec.get("lookup") not in _FILTER_LOOKUPS for spec in captured.get("filters", ())):
        raise ValueError("captured filter lookup requires manual adaptation")
    pagination = captured.get("pagination")
    if pagination and pagination.get("kind") not in {"cursor", "page", "limit-offset"}:
        raise ValueError("captured pagination requires manual adaptation")
    return captured


def capture_listing(
    view_class: type[Any], captured_listing: dict[str, Any] | None
) -> dict[str, Any]:
    """Return a complete qualified listing contract or reject the source behavior."""

    if captured_listing:
        listing = _validate_captured_listing(captured_listing)
    else:
        listing = _capture_backends(view_class)
        if pagination := capture_stock_pagination(view_class):
            listing["pagination"] = pagination
        listing = _validate_captured_listing(listing)
    pagination = listing.get("pagination") or {}
    if pagination.get("kind") == "cursor":
        serializers = importlib.import_module("rest_framework.serializers")
        fields, _text, pk = _serializer_fields(view_class)
        for term in pagination.get("ordering", ()):
            name = str(term).lstrip("-")
            field = fields.get(pk if name == "pk" else name)
            zone = getattr(field, "timezone", None)
            datetime_supported = (
                isinstance(field, serializers.DateTimeField)
                and (zone if zone is not None else field.default_timezone()) is not None
            )
            if not isinstance(field, serializers.IntegerField) and not datetime_supported:
                raise ValueError("cursor ordering field kind requires manual adaptation")
    return listing


def _value(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    return str(values[-1]) if values else None


def _field(resource: dict[str, Any], name: str) -> dict[str, Any]:
    if name == "pk":
        name = str(resource.get("pk_column") or "id")
    for field in cast(list[dict[str, Any]], resource["fields"]):
        column = str(field.get("column") or field.get("attname") or field["name"])
        if name in {field["name"], column}:
            return field
    if name == resource.get("pk_column"):
        return {"name": name, "column": name, "kind": "integer"}
    raise ValueError(f"unqualified list field: {name}")


def _column(table: Any, resource: dict[str, Any], name: str) -> Any:
    field = _field(resource, name)
    return table.c[str(field.get("column") or field.get("attname") or field["name"])]


def _coerce(field: dict[str, Any], value: str) -> Any:
    kind = str(field.get("kind") or "char")
    if kind in {"integer", "big_integer", "related_pk"}:
        return int(value)
    if kind == "decimal":
        return Decimal(value)
    if kind == "boolean":
        lowered = value.lower()
        if lowered in {"true", "1"}:
            return True
        if lowered in {"false", "0"}:
            return False
        raise ValueError("invalid boolean filter value")
    if kind == "datetime":
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    if kind == "date":
        return date.fromisoformat(value)
    if kind == "uuid":
        return UUID(value)
    return value


class _FilterError(ValueError):
    def __init__(self, body: dict[str, list[str]]) -> None:
        self.body = body


def _filter_value(spec: dict[str, Any], field: dict[str, Any], raw: str) -> Any:
    kind = spec.get("value_kind")
    if kind is None:
        return _coerce(field, raw)
    if kind == "boolean":
        lowered = raw.lower()
        if lowered in {"true", "1"}:
            return True
        if lowered in {"false", "0"}:
            return False
        return None
    if kind == "number":
        try:
            value = Decimal(raw.strip())
            if not value.is_finite():
                raise ValueError
        except (ArithmeticError, ValueError):
            raise _FilterError({str(spec["param"]): [str(spec["invalid"])]}) from None
        maximum = spec.get("max_value")
        if maximum is not None and value > Decimal(str(maximum)):
            raise _FilterError({str(spec["param"]): [str(spec["max_value_error"])]})
        return int(value) if spec.get("number_kind") == "integer" else value
    if kind == "choice" and raw not in spec.get("choices", ()):
        template = str(spec["invalid_choice"])
        raise _FilterError({str(spec["param"]): [template % {"value": raw}]})
    if kind == "string":
        if "\x00" in raw:
            raise _FilterError({str(spec["param"]): [str(spec["null_character_error"])]})
        return raw.strip()
    return raw


def _search_terms(value: str) -> list[str]:
    terms: list[str] = []
    for match in _SMART_SPLIT.finditer(value):
        term = match[0].strip(",")
        if term.startswith(('"', "'")) and term[0] == term[-1]:
            quote = term[0]
            terms.append(term[1:-1].replace("\\" + quote, quote).replace("\\\\", "\\"))
        else:
            terms.extend(part.strip() for part in term.split(",") if part)
    return terms


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _integer_expression(sa: Any, column: Any, lookup: str, value: Any, bounds: Any) -> Any:
    minimum, maximum = bounds
    if minimum <= value <= maximum:
        return None
    if lookup == "exact":
        return sa.false()
    all_rows = (lookup in {"gt", "gte"} and value < minimum) or (
        lookup in {"lt", "lte"} and value > maximum
    )
    return sa.true() if all_rows else sa.false()


def _filters(
    statement: Any,
    table: Any,
    resource: dict[str, Any],
    listing: dict[str, Any],
    query: dict[str, list[str]],
) -> Any:
    import sqlalchemy as sa

    cleaned: list[tuple[dict[str, Any], Any]] = []
    errors: dict[str, list[str]] = {}
    for spec in listing.get("filters") or ():
        lookup = str(spec.get("lookup") or "exact")
        if lookup not in {"exact", "iexact", "in", "gt", "gte", "lt", "lte", "isnull"}:
            raise ValueError(f"unsupported filter lookup: {lookup}")
        raw = _value(query, str(spec["param"]))
        if raw is None or raw == "":
            continue
        name = str(spec["name"])
        field = _field(resource, name)
        value: Any
        try:
            if lookup == "in":
                value = [
                    None
                    if item == "" and spec.get("value_kind") == "number"
                    else _filter_value(spec, field, item)
                    for item in raw.split(",")
                ]
            else:
                value = _filter_value(spec, field, raw)
        except _FilterError as error:
            errors.update(error.body)
            continue
        if value is None or value == [] or (spec.get("value_kind") == "string" and value == ""):
            continue
        cleaned.append((spec, value))
    if errors:
        raise _FilterError(errors)

    for spec, value in cleaned:
        lookup = str(spec.get("lookup") or "exact")
        column = _column(table, resource, str(spec["name"]))
        expression = (
            _integer_expression(sa, column, lookup, value, spec["integer_range"])
            if spec.get("number_kind") == "integer"
            and lookup in {"exact", "gt", "gte", "lt", "lte"}
            else None
        )
        if expression is not None:
            pass
        elif lookup == "in":
            if not value:
                continue
            expression = column.in_(value)
        elif lookup == "exact":
            expression = column == value
        elif lookup == "iexact":
            expression = column.ilike(_escape_like(str(value)), escape="\\")
        elif lookup in {"gt", "gte", "lt", "lte"}:
            expression = {
                "gt": column > value,
                "gte": column >= value,
                "lt": column < value,
                "lte": column <= value,
            }[lookup]
        elif lookup == "isnull":
            expression = column.is_(None) if value else column.is_not(None)
        statement = statement.where(expression)
    search = listing.get("search")
    if search:
        terms = _search_terms(_value(query, str(search["param"])) or "")
        fields = list(search.get("fields") or ())
        unsupported = [
            spec["lookup"]
            for spec in fields
            if spec["lookup"] not in {"icontains", "istartswith", "iexact"}
        ]
        if unsupported:
            raise ValueError(f"unsupported search lookup: {unsupported[0]}")
        for term in terms:
            matches = []
            for spec in fields:
                column = _column(table, resource, str(spec["name"]))
                escaped = _escape_like(term)
                lookup = spec["lookup"]
                if lookup == "icontains":
                    pattern = f"%{escaped}%"
                elif lookup == "istartswith":
                    pattern = f"{escaped}%"
                elif lookup == "iexact":
                    pattern = escaped
                matches.append(column.ilike(pattern, escape="\\"))
            if matches:
                statement = statement.where(sa.or_(*matches))
    return statement


def apply_filters(
    statement: Any,
    table: Any,
    resource: dict[str, Any],
    listing: dict[str, Any],
    query: dict[str, list[str]],
) -> tuple[Any, dict[str, list[str]] | None]:
    """Apply captured stock filters and preserve DRF's validation body."""

    try:
        return _filters(statement, table, resource, listing, query), None
    except _FilterError as error:
        return statement, error.body


def _requested_ordering(
    config: dict[str, Any] | None, query: dict[str, list[str]]
) -> list[str] | None:
    if not config:
        return None
    raw = _value(query, str(config["param"]))
    chosen: list[str] | None = None
    if raw:
        valid = set(config["fields"])
        kept = [
            term for item in raw.split(",") if (term := item.strip()) and term.lstrip("-") in valid
        ]
        if kept:
            chosen = kept
    if chosen is None and config.get("default"):
        chosen = [str(item) for item in config["default"]]
    if chosen and config.get("rule") in {"append-pk-follow-last", "append-pk-asc"}:
        pk = str(config.get("pk") or "id")
        if pk not in {term.lstrip("-") for term in chosen} and "pk" not in {
            term.lstrip("-") for term in chosen
        }:
            descending = config["rule"] == "append-pk-follow-last" and chosen[-1].startswith("-")
            chosen.append(("-" if descending else "") + pk)
    return chosen


def _ordered(statement: Any, table: Any, resource: dict[str, Any], ordering: list[str]) -> Any:
    for term in ordering:
        column = _column(table, resource, term.lstrip("-"))
        statement = statement.order_by(column.desc() if term.startswith("-") else column.asc())
    return statement


def _positive_int(raw: str | None, default: int, *, strict: bool, cutoff: int | None = None) -> int:
    try:
        number = int(raw) if raw is not None else default
        if number < 0 or (strict and number == 0):
            raise ValueError
    except ValueError:
        number = default
    return min(number, cutoff) if cutoff else number


def _replace_query(url: str, key: str, value: str | None) -> str:
    scheme, netloc, path, raw_query, fragment = urlparse.urlsplit(url)
    params = urlparse.parse_qs(raw_query, keep_blank_values=True)
    if value is None:
        params.pop(key, None)
    else:
        params[key] = [value]
    query = urlparse.urlencode(sorted(params.items()), doseq=True)
    return urlparse.urlunsplit((scheme, netloc, path, query, fragment))


def _rows(session: Any, statement: Any) -> list[Any]:
    return list(session.execute(statement).mappings())


def _count(session: Any, statement: Any) -> int:
    import sqlalchemy as sa

    counted = statement.order_by(None).subquery()
    return int(session.execute(sa.select(sa.func.count()).select_from(counted)).scalar_one())


def _page_number(
    session: Any, statement: Any, config: dict[str, Any], query: dict[str, list[str]], url: str
) -> tuple[list[Any] | dict[str, str], int, int, str | None, str | None]:
    count = _count(session, statement)
    size = _positive_int(
        _value(query, str(config["page_size_param"])) if config.get("page_size_param") else None,
        int(config["page_size"]),
        strict=True,
        cutoff=config.get("max_page_size"),
    )
    pages = max(1, math.ceil(count / size))
    raw = _value(query, str(config["page_param"])) or "1"
    if raw in config.get("last_page_strings", ["last"]):
        page = pages
    else:
        try:
            page = int(raw)
        except ValueError:
            page = 0
    if page < 1 or page > pages or (count == 0 and page != 1):
        return (
            {"detail": str(config.get("invalid_page_message") or "Invalid page.")},
            404,
            count,
            None,
            None,
        )
    rows = _rows(session, statement.offset((page - 1) * size).limit(size))
    param = str(config["page_param"])
    next_link = _replace_query(url, param, str(page + 1)) if page < pages else None
    previous = (
        _replace_query(url, param, None if page == 2 else str(page - 1)) if page > 1 else None
    )
    return rows, 200, count, next_link, previous


def _limit_offset(
    session: Any, statement: Any, config: dict[str, Any], query: dict[str, list[str]], url: str
) -> tuple[list[Any], int, str | None, str | None]:
    count = _count(session, statement)
    limit = _positive_int(
        _value(query, str(config["limit_param"])),
        int(config["default_limit"]),
        strict=True,
        cutoff=config.get("max_limit"),
    )
    offset = _positive_int(_value(query, str(config["offset_param"])), 0, strict=False)
    rows = _rows(session, statement.offset(offset).limit(limit))
    link_url = _replace_query(url, str(config["limit_param"]), str(limit))
    next_link = (
        _replace_query(link_url, str(config["offset_param"]), str(offset + limit))
        if offset + limit < count
        else None
    )
    if offset == 0:
        previous = None
    else:
        previous_offset = max(0, offset - limit)
        previous = _replace_query(
            link_url,
            str(config["offset_param"]),
            None if previous_offset == 0 else str(previous_offset),
        )
    return rows, count, next_link, previous


def _position(resource: dict[str, Any], row: Any, ordering: list[str]) -> str:
    field = _field(resource, ordering[0].lstrip("-"))
    value = row[str(field.get("column") or field.get("attname") or field["name"])]
    if field.get("kind") == "decimal":
        places = int(field.get("decimal_places") or 0)
        return str(Decimal(str(value)).quantize(Decimal(1).scaleb(-places)))
    if field.get("kind") == "datetime":
        if field.get("timezone"):
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            value = value.astimezone(UTC)
        elif value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
    return str(value)


def _encode_cursor(url: str, param: str, offset: int, reverse: bool, position: str | None) -> str:
    tokens: dict[str, str] = {}
    if offset:
        tokens["o"] = str(offset)
    if reverse:
        tokens["r"] = "1"
    if position is not None:
        tokens["p"] = position
    encoded = base64.b64encode(urlparse.urlencode(tokens).encode("ascii")).decode("ascii")
    return _replace_query(url, param, encoded)


def _decode_cursor(encoded: str, config: dict[str, Any]) -> dict[str, Any]:
    if not _CURSOR.fullmatch(encoded):
        raise ValueError
    raw = base64.b64decode(encoded.encode("ascii"), validate=True).decode("ascii")
    tokens = urlparse.parse_qs(raw, keep_blank_values=True)
    offset = _positive_int(
        tokens.get("o", ["0"])[0], 0, strict=False, cutoff=int(config.get("offset_cutoff") or 1000)
    )
    reverse = bool(int(tokens.get("r", ["0"])[0]))
    return {"offset": offset, "reverse": reverse, "position": tokens.get("p", [None])[0]}


def _cursor_links(
    resource: dict[str, Any], state: dict[str, Any], url: str, param: str
) -> tuple[str | None, str | None]:
    page = state["page"]
    cursor = state["cursor"]
    ordering = state["ordering"]
    page_size = state["page_size"]
    next_link = None
    if state["has_next"]:
        compare = (
            _position(resource, page[-1], ordering)
            if page and cursor and cursor["reverse"] and cursor["offset"]
            else state["next_position"]
        )
        offset, position, unique = 0, None, False
        for index, item in enumerate(reversed(page)):
            position = _position(resource, item, ordering)
            if position != compare:
                unique = True
                offset = index
                break
            compare = position
        else:
            offset = len(page)
        if page and not unique:
            if not state["has_previous"]:
                offset, position = page_size, None
            elif cursor and cursor["reverse"]:
                offset, position = 0, state["previous_position"]
            else:
                offset = (cursor["offset"] if cursor else 0) + page_size
                position = state["previous_position"]
        if not page:
            position = state["next_position"]
        next_link = _encode_cursor(url, param, offset, False, position)
    previous_link = None
    if state["has_previous"]:
        compare = (
            _position(resource, page[0], ordering)
            if page and cursor and not cursor["reverse"] and cursor["offset"]
            else state["previous_position"]
        )
        offset, position, unique = 0, None, False
        for index, item in enumerate(page):
            position = _position(resource, item, ordering)
            if position != compare:
                unique = True
                offset = index
                break
            compare = position
        else:
            offset = len(page)
        if page and not unique:
            if not state["has_next"]:
                offset, position = page_size, None
            elif cursor and cursor["reverse"]:
                offset = cursor["offset"] + page_size
                position = state["next_position"]
            else:
                offset, position = 0, state["next_position"]
        if not page:
            position = state["previous_position"]
        previous_link = _encode_cursor(url, param, offset, True, position)
    return next_link, previous_link


def _cursor_page(
    session: Any,
    statement: Any,
    table: Any,
    resource: dict[str, Any],
    ordering: list[str],
    query: dict[str, list[str]],
    url: str,
    config: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    page_size = _positive_int(
        _value(query, str(config["page_size_param"])) if config.get("page_size_param") else None,
        int(config["page_size"]),
        strict=True,
        cutoff=config.get("max_page_size"),
    )
    param = str(config["cursor_param"])
    encoded = _value(query, param)
    cursor = None
    if encoded is not None:
        try:
            cursor = _decode_cursor(encoded, config)
        except (UnicodeError, ValueError):
            return None, {"detail": str(config["invalid_cursor_message"])}
    offset = cursor["offset"] if cursor else 0
    reverse = cursor["reverse"] if cursor else False
    position = cursor["position"] if cursor else None
    effective = (
        [term[1:] if term.startswith("-") else "-" + term for term in ordering]
        if reverse
        else ordering
    )
    if position is not None:
        first = ordering[0]
        field = _field(resource, first.lstrip("-"))
        column = _column(table, resource, first.lstrip("-"))
        marker = _coerce(field, position)
        keep_lower = reverse != first.startswith("-")
        statement = statement.where(column < marker if keep_lower else column > marker)
    rows = _rows(
        session, _ordered(statement, table, resource, effective).offset(offset).limit(page_size + 1)
    )
    page = rows[:page_size]
    following = _position(resource, rows[-1], ordering) if len(rows) > len(page) else None
    if reverse:
        page.reverse()
        has_next = position is not None or offset > 0
        has_previous = following is not None
        next_position = position if has_next else None
        previous_position = following
    else:
        has_next = following is not None
        has_previous = position is not None or offset > 0
        next_position = following
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
    next_link, previous_link = _cursor_links(resource, state, url, param)
    return {"rows": page, "next": next_link, "previous": previous_link}, None


def list_records(
    session: Any,
    table: Any,
    resource: dict[str, Any],
    listing: dict[str, Any],
    query: dict[str, list[str]],
    url: str,
    serialize: Callable[[Any], dict[str, Any]],
    *,
    base_statement: Any | None = None,
) -> tuple[Any, int]:
    """Execute one qualified list request with bounded SQL fetching."""

    import sqlalchemy as sa

    statement, filter_error = apply_filters(
        sa.select(table) if base_statement is None else base_statement,
        table,
        resource,
        listing,
        query,
    )
    if filter_error:
        return filter_error, 400
    requested = _requested_ordering(listing.get("ordering"), query)
    pagination = listing.get("pagination")
    if pagination and pagination.get("kind") == "cursor":
        ordering = requested or [str(term) for term in pagination["ordering"]]
        page, error = _cursor_page(
            session, statement, table, resource, ordering, query, url, pagination
        )
        if error:
            return error, 404
        assert page is not None
        return {
            "next": page["next"],
            "previous": page["previous"],
            "results": [serialize(row) for row in page["rows"]],
        }, 200
    ordering = requested or [str(term) for term in resource.get("ordering") or ()]
    statement = _ordered(statement, table, resource, ordering)
    if pagination and pagination.get("kind") == "page":
        rows, status, count, next_link, previous = _page_number(
            session, statement, pagination, query, url
        )
        if status != 200:
            return rows, status
        return {
            "count": count,
            "next": next_link,
            "previous": previous,
            "results": [serialize(row) for row in rows],
        }, 200
    if pagination and pagination.get("kind") == "limit-offset":
        rows, count, next_link, previous = _limit_offset(session, statement, pagination, query, url)
        return {
            "count": count,
            "next": next_link,
            "previous": previous,
            "results": [serialize(row) for row in rows],
        }, 200
    if pagination:
        raise ValueError(f"unsupported pagination kind: {pagination.get('kind')}")
    return [serialize(row) for row in _rows(session, statement)], 200

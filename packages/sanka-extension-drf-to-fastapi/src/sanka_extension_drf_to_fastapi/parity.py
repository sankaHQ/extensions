# SPDX-License-Identifier: Apache-2.0
"""Per-route parity notes: exact source behavior a port must reproduce.

Bench v5 showed that 22 of 27 agent failures were near-misses in four families of
DRF semantics — authentication ordering and exact error strings, conditional
responses, pagination and ordering tie-breaks, multipart and file rules, and
unique-conflict wording. The scanner already visits the constructs that produce
them; this module turns those visits into machine-readable facts derived from the
live installation (never guessed), so an agent reads them instead of rediscovering
them by probing the source application.

Every note carries a family, a stable code, a message, and the project source
location when it points at project code. Note generation never fails a scan: a
family that cannot be derived reports ``SANKA_DRF_PARITY_UNAVAILABLE`` with the
reason instead of raising.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
import textwrap
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import ParityNote

FAMILY_AUTH = "auth"
FAMILY_CONDITIONAL = "conditional"
FAMILY_PAGINATION = "pagination"
FAMILY_ORDERING = "ordering"
FAMILY_FILTERING = "filtering"
FAMILY_MULTIPART = "multipart"
FAMILY_UNIQUENESS = "uniqueness"
FAMILY_NULLABILITY = "nullability"
FAMILY_MESSAGES = "messages"
FAMILY_ROUTING = "routing"
FAMILY_OVERRIDES = "overrides"

FAMILIES = (
    FAMILY_ROUTING,
    FAMILY_AUTH,
    FAMILY_CONDITIONAL,
    FAMILY_PAGINATION,
    FAMILY_ORDERING,
    FAMILY_FILTERING,
    FAMILY_MULTIPART,
    FAMILY_UNIQUENESS,
    FAMILY_NULLABILITY,
    FAMILY_MESSAGES,
    FAMILY_OVERRIDES,
)

_WRITE_METHODS = {"POST", "PUT", "PATCH"}
_SENTENCE = re.compile(r"^[A-Z][^\n]{7,}[.!?]$")
_CONDITIONAL_TOKENS = (
    "ETag",
    "If-None-Match",
    "If-Match",
    "Last-Modified",
    "If-Modified-Since",
    "If-Unmodified-Since",
    "HTTP_304_NOT_MODIFIED",
    "HTTP_412_PRECONDITION_FAILED",
    "condition(",
    "@etag",
    "@last_modified",
    "Cache-Control",
)
_HEADER_LITERAL = re.compile(r"[\"']([A-Z][A-Za-z]+(?:-[A-Za-z]+)+|ETag|Vary)[\"']\s*:")
_MESSAGE_KEYS = (
    "required",
    "null",
    "blank",
    "invalid",
    "max_length",
    "min_length",
    "min_value",
    "max_value",
    "max_digits",
    "max_decimal_places",
    "max_whole_digits",
    "max_string_length",
    "invalid_choice",
    "not_a_list",
    "empty",
    "no_name",
    "invalid_image",
    "does_not_exist",
    "incorrect_type",
    "not_a_dict",
)
_VIEW_OVERRIDES = (
    "list",
    "retrieve",
    "create",
    "update",
    "partial_update",
    "destroy",
    "perform_create",
    "perform_update",
    "perform_destroy",
    "get_queryset",
    "get_object",
    "get_serializer_class",
    "get_serializer",
    "filter_queryset",
    "paginate_queryset",
    "get_paginated_response",
    "finalize_response",
    "handle_exception",
    "initial",
    "check_permissions",
    "check_object_permissions",
    "get_permissions",
    "get_authenticators",
    "get_parsers",
    "get_renderers",
)
_SERIALIZER_OVERRIDES = (
    "create",
    "update",
    "save",
    "validate",
    "to_internal_value",
    "to_representation",
    "run_validation",
    "get_fields",
)


@dataclass(frozen=True, slots=True)
class _Context:
    view_class: type[Any]
    callback: Any
    actions: dict[str, str] | None
    method: str
    operation: str
    path: str
    middleware: tuple[str, ...]
    root: Path

    @property
    def is_collection(self) -> bool:
        return "{" not in self.path

    @property
    def writes(self) -> bool:
        return self.method in _WRITE_METHODS


def route_parity_notes(
    *,
    view_class: type[Any],
    callback: Any,
    actions: dict[str, str] | None,
    method: str,
    operation: str,
    path: str,
    middleware: tuple[str, ...],
    root_path: Path,
) -> tuple[ParityNote, ...]:
    """Derive every parity note for one method-route from the live view class."""
    context = _Context(
        view_class=view_class,
        callback=callback,
        actions=actions,
        method=method,
        operation=operation,
        path=path,
        middleware=middleware,
        root=root_path,
    )
    producers: tuple[tuple[str, Callable[[_Context], Iterable[ParityNote]]], ...] = (
        (FAMILY_ROUTING, _routing_notes),
        (FAMILY_AUTH, _auth_notes),
        (FAMILY_CONDITIONAL, _conditional_notes),
        (FAMILY_PAGINATION, _pagination_notes),
        (FAMILY_ORDERING, _ordering_notes),
        (FAMILY_FILTERING, _filtering_notes),
        (FAMILY_MULTIPART, _multipart_notes),
        (FAMILY_UNIQUENESS, _uniqueness_notes),
        (FAMILY_NULLABILITY, _nullability_notes),
        (FAMILY_MESSAGES, _messages_notes),
        (FAMILY_OVERRIDES, _override_notes),
    )
    notes: list[ParityNote] = []
    for family, producer in producers:
        try:
            notes.extend(producer(context))
        except Exception as error:
            notes.append(
                ParityNote(
                    family=family,
                    code="SANKA_DRF_PARITY_UNAVAILABLE",
                    message=(
                        f"{family} notes could not be derived: {type(error).__name__}: {error}"
                    ),
                )
            )
    return tuple(notes)


# --- shared helpers -----------------------------------------------------------


def _note(family: str, code: str, message: str, source: str | None = None) -> ParityNote:
    return ParityNote(family=family, code=code, message=message, source=source)


def _qualified(value: Any) -> str:
    module = getattr(value, "__module__", None)
    name = getattr(value, "__qualname__", None) or getattr(value, "__name__", None)
    if module and name:
        return f"{module}.{name}"
    return str(value)


def _source(value: Any) -> str:
    try:
        return inspect.getsource(value)
    except (OSError, TypeError):
        return ""


def _location(value: Any, root: Path) -> str | None:
    """``file:line`` for project code; None for framework code or unknown origins."""
    try:
        file = Path(inspect.getsourcefile(value) or "").resolve()
        _, line = inspect.getsourcelines(value)
    except (OSError, TypeError, ValueError):
        return None
    try:
        relative = file.relative_to(root)
    except ValueError:
        return None
    return f"{relative}:{line}"


def _is_project_code(value: Any, root: Path) -> bool:
    return _location(value, root) is not None


def _sentences(value: Any) -> tuple[str, ...]:
    """Sentence-like string literals in ``value``'s source, docstrings excluded."""
    source = _source(value)
    if not source:
        return ()
    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        return ()
    docstrings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            text = ast.get_docstring(node, clean=False)
            if text:
                docstrings.add(text)
    found: list[str] = []
    for node in ast.walk(tree):
        rendered: str | None = None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            rendered = node.value
        elif isinstance(node, ast.JoinedStr):
            rendered = "".join(
                part.value
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
                else "{…}"
                for part in node.values
            )
        if rendered is None or rendered in docstrings or rendered in found:
            continue
        if _SENTENCE.match(rendered):
            found.append(rendered)
    return tuple(found)


def _own_methods(cls: type[Any], names: Iterable[str], root: Path) -> list[tuple[str, Any]]:
    """Methods ``cls`` (or a project base class) defines itself, in MRO order."""
    wanted = set(names)
    seen: set[str] = set()
    found: list[tuple[str, Any]] = []
    for klass in inspect.getmro(cls):
        if not _is_project_code(klass, root):
            continue
        for name, member in vars(klass).items():
            if name in wanted and name not in seen and inspect.isfunction(member):
                seen.add(name)
                found.append((name, member))
    return found


def _default_detail(name: str, **params: Any) -> str:
    exceptions_module = importlib.import_module("rest_framework.exceptions")
    template = str(getattr(exceptions_module, name).default_detail)
    try:
        return template.format(**params) if params else template
    except (IndexError, KeyError):
        return template


def _view_instance(context: _Context) -> Any | None:
    """Best-effort instance shaped the way the router would build it."""
    initkwargs = dict(getattr(context.callback, "initkwargs", None) or {})
    allowed = {"suffix", "detail", "basename", "name", "description"}
    kwargs = {key: value for key, value in initkwargs.items() if key in allowed}
    try:
        view = context.view_class(**kwargs)
    except Exception:
        try:
            view = context.view_class()
        except Exception:
            return None
    view.action = context.operation if context.actions is not None else None
    if context.actions is not None:
        view.action_map = dict(context.actions)
    view.request = None
    view.args = ()
    view.kwargs = {}
    view.format_kwarg = None
    return view


def _permission_classes(context: _Context, view: Any | None) -> list[type[Any]]:
    if view is not None:
        try:
            return [type(item) for item in view.get_permissions()]
        except Exception:
            pass
    return [item for item in getattr(context.view_class, "permission_classes", ()) if item]


def _authenticator_classes(context: _Context, view: Any | None) -> list[type[Any]]:
    if view is not None:
        try:
            return [type(item) for item in view.get_authenticators()]
        except Exception:
            pass
    return [item for item in getattr(context.view_class, "authentication_classes", ()) if item]


def _serializer_class(context: _Context, view: Any | None) -> type[Any] | None:
    if view is not None:
        try:
            candidate = view.get_serializer_class()
        except Exception:
            candidate = None
        if inspect.isclass(candidate):
            return candidate
    candidate = getattr(context.view_class, "serializer_class", None)
    return candidate if inspect.isclass(candidate) else None


def _serializer_fields(serializer_class: type[Any] | None) -> list[tuple[str, Any]]:
    """Bound ``(name, field)`` pairs, or ``[]`` when the serializer cannot be built."""
    if serializer_class is None:
        return []
    try:
        serializer = serializer_class()
        return list(serializer.fields.items())
    except Exception:
        return []


def _walk_fields(
    fields: list[tuple[str, Any]], prefix: str = ""
) -> Iterator[tuple[str, Any, str | None]]:
    """Yield ``(label, field, container)``; container is 'list' inside many=True."""
    serializers_module = importlib.import_module("rest_framework.serializers")
    for name, field in fields:
        label = f"{prefix}{name}"
        if isinstance(field, serializers_module.ListSerializer):
            yield label, field, "list"
            child = field.child
            if isinstance(child, serializers_module.BaseSerializer):
                yield from _walk_fields(_bound_fields(child), f"{label}[].")
        elif isinstance(field, serializers_module.BaseSerializer):
            yield label, field, "nested"
            yield from _walk_fields(_bound_fields(field), f"{label}.")
        else:
            yield label, field, None


def _bound_fields(serializer: Any) -> list[tuple[str, Any]]:
    try:
        return list(serializer.fields.items())
    except Exception:
        return []


_CONSTRAINT_KEYS = {
    "max_length": "max_length",
    "min_length": "min_length",
    "min_value": "min_value",
    "max_value": "max_value",
    "max_digits": "max_digits",
    "max_decimal_places": "decimal_places",
    "max_whole_digits": "max_digits",
}


def _format_message(field: Any, key: str) -> str | None:
    template = field.error_messages.get(key)
    if not template:
        return None
    constraint = _CONSTRAINT_KEYS.get(key)
    if constraint is not None and getattr(field, constraint, None) is None:
        return None
    params = {
        name: value
        for name in ("min_value", "max_value", "max_length", "min_length", "max_digits")
        if (value := getattr(field, name, None)) is not None
    }
    decimal_places = getattr(field, "decimal_places", None)
    if decimal_places is not None:
        params["max_decimal_places"] = decimal_places
        if getattr(field, "max_digits", None) is not None:
            params["max_whole_digits"] = field.max_digits - decimal_places
    text = str(template)
    try:
        return text.format(**params)
    except (IndexError, KeyError):
        return text


def _quote(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'


def _join(values: Iterable[str]) -> str:
    return ", ".join(values)


# --- routing ------------------------------------------------------------------


def _allowed_methods(context: _Context) -> list[str]:
    cls = context.view_class
    names = [name for name in getattr(cls, "http_method_names", ()) if name != "trace"]
    if context.actions is not None:
        actions = context.actions
        allowed = [
            name
            for name in names
            if name in actions or name == "options" or (name == "head" and "get" in actions)
        ]
    else:
        allowed = [
            name
            for name in names
            if name == "options"
            or callable(getattr(cls, name, None))
            or (name == "head" and callable(getattr(cls, "get", None)))
        ]
    return [name.upper() for name in allowed]


def _routing_notes(context: _Context) -> Iterator[ParityNote]:
    allowed = _allowed_methods(context)
    if allowed:
        yield _note(
            FAMILY_ROUTING,
            "SANKA_DRF_PARITY_ALLOWED_METHODS",
            f"Allowed methods on this path: {_join(allowed)}. Any other method answers "
            f'405 {{"detail": {_quote(_default_detail("MethodNotAllowed", method="X"))}}} '
            "(X = the request method) with an Allow header listing exactly these; HEAD "
            "mirrors GET with an empty body.",
        )
    view = _view_instance(context)
    if view is not None:
        try:
            name = str(view.get_view_name())
            description = str(view.get_view_description())
            renders = [str(item.media_type) for item in view.renderer_classes]
            parses = [str(item.media_type) for item in view.parser_classes]
        except Exception:
            name = ""
            description = ""
            renders = []
            parses = []
        if name:
            yield _note(
                FAMILY_ROUTING,
                "SANKA_DRF_PARITY_OPTIONS_METADATA",
                "OPTIONS answers 200 with DRF's metadata body "
                f'{{"name": {_quote(name)}, "description": {_quote(description)}, '
                f'"renders": [{_join(_quote(item) for item in renders)}], '
                f'"parses": [{_join(_quote(item) for item in parses)}]}}; an "actions" '
                "map of field metadata is added for POST/PUT only when that method is "
                "allowed here and the caller passes the permission checks.",
            )
    if not context.is_collection:
        regex = str(getattr(context.view_class, "lookup_value_regex", "[^/.]+") or "[^/.]+")
        yield _note(
            FAMILY_ROUTING,
            "SANKA_DRF_PARITY_MISSING_OBJECT",
            "Lookup values must match the router pattern "
            f"{_quote(regex)} to reach the view at all; a value that matches but names no "
            f'row answers 404 {{"detail": {_quote(_default_detail("NotFound"))}}} after '
            "authentication and view-level permission checks, and before object-level "
            "permission checks.",
        )
    common = "django.middleware.common.CommonMiddleware"
    settings = importlib.import_module("django.conf").settings
    if (
        context.path.endswith("/")
        and len(context.path) > 1
        and common in context.middleware
        and bool(getattr(settings, "APPEND_SLASH", True))
    ):
        yield _note(
            FAMILY_ROUTING,
            "SANKA_DRF_PARITY_APPEND_SLASH",
            f"Requests for {context.path.rstrip('/')} (no trailing slash) answer 301 with "
            f"Location: {context.path} and an empty body for every method "
            "(CommonMiddleware APPEND_SLASH); the slashed path is the only one that serves.",
        )
    elif context.path.endswith("/") and len(context.path) > 1:
        yield _note(
            FAMILY_ROUTING,
            "SANKA_DRF_PARITY_NO_APPEND_SLASH",
            f"CommonMiddleware is not active, so {context.path.rstrip('/')} (no trailing "
            "slash) is not redirected: it answers Django's plain 404, not a JSON detail.",
        )


# --- authentication and permissions -----------------------------------------


def _authenticate_header(auth_class: type[Any]) -> str | None:
    try:
        value = auth_class().authenticate_header(None)
    except Exception:
        return "unknown"
    return str(value) if value else None


def _auth_notes(context: _Context) -> Iterator[ParityNote]:
    permissions_module = importlib.import_module("rest_framework.permissions")
    authentication_module = importlib.import_module("rest_framework.authentication")
    view = _view_instance(context)
    permissions = _permission_classes(context, view)
    authenticators = _authenticator_classes(context, view)
    restrictive = [item for item in permissions if item is not permissions_module.AllowAny]
    dynamic = _own_methods(
        context.view_class, ("get_permissions", "get_authenticators"), context.root
    )
    if dynamic:
        names = _join(name for name, _member in dynamic)
        yield _note(
            FAMILY_AUTH,
            "SANKA_DRF_PARITY_DYNAMIC_PERMISSIONS",
            f"{names} is overridden, so the classes below were evaluated for this "
            f"operation ({context.operation}) and can differ on the other routes of the "
            f"view: permissions {_join(_qualified(item) for item in permissions) or 'none'}; "
            f"authenticators {_join(_qualified(item) for item in authenticators) or 'none'}.",
            _location(dynamic[0][1], context.root),
        )
    if authenticators:
        first_header = _authenticate_header(authenticators[0])
        yield _note(
            FAMILY_AUTH,
            "SANKA_DRF_PARITY_AUTH_ORDER",
            "Authenticators run in order "
            f"[{_join(_qualified(item) for item in authenticators)}] on every request, "
            "before permission checks and before object lookup; a malformed or unknown "
            "credential fails with 401 even when the route allows anonymous access.",
        )
        if restrictive:
            if first_header and first_header != "unknown":
                unauthenticated = (
                    "401 with WWW-Authenticate: "
                    f'{first_header} and body {{"detail": '
                    f"{_quote(_default_detail('NotAuthenticated'))}}}"
                )
            elif first_header is None:
                unauthenticated = (
                    "403 (the first authenticator sends no WWW-Authenticate header, so "
                    'DRF downgrades 401 to 403) with body {"detail": '
                    f"{_quote(_default_detail('NotAuthenticated'))}}}"
                )
            else:
                unauthenticated = (
                    "401 or 403 depending on the first authenticator's "
                    'authenticate_header(), with body {"detail": '
                    f"{_quote(_default_detail('NotAuthenticated'))}}}"
                )
            yield _note(
                FAMILY_AUTH,
                "SANKA_DRF_PARITY_UNAUTHENTICATED",
                f"Permissions [{_join(_qualified(item) for item in permissions)}]: a request "
                f"without valid credentials answers {unauthenticated}; a missing object is "
                "not consulted first, so unauthenticated requests for absent rows get this "
                "answer, not 404.",
            )
            yield _note(
                FAMILY_AUTH,
                "SANKA_DRF_PARITY_FORBIDDEN",
                "An authenticated caller that fails a permission answers 403 "
                f'{{"detail": {_quote(_default_detail("PermissionDenied"))}}} unless the '
                "failing permission class defines `message`; object-level checks "
                "(has_object_permission) run only after the object was found, so a "
                "non-owner asking for an absent row gets 404.",
            )
    for permission in restrictive:
        if not _is_project_code(permission, context.root):
            if permission is permissions_module.IsAdminUser:
                yield _note(
                    FAMILY_AUTH,
                    "SANKA_DRF_PARITY_PERMISSION_BUILTIN",
                    "IsAdminUser passes only users with is_staff=True; any other "
                    "authenticated user is forbidden (403).",
                )
            continue
        hooks = [
            name for name in ("has_permission", "has_object_permission") if name in vars(permission)
        ]
        message = getattr(permission, "message", None)
        detail = (
            f"; its `message` replaces the default detail: {_quote(str(message))}"
            if message
            else ""
        )
        yield _note(
            FAMILY_AUTH,
            "SANKA_DRF_PARITY_PERMISSION_CUSTOM",
            f"{_qualified(permission)} implements {_join(hooks) or 'no hooks'}"
            + (
                " (object-level only: it never blocks list/create, and a missing object "
                "still answers 404)"
                if hooks == ["has_object_permission"]
                else ""
            )
            + detail
            + ".",
            _location(permission, context.root),
        )
    for auth_class in authenticators:
        yield from _authenticator_notes(context, auth_class, authentication_module)


def _authenticator_notes(
    context: _Context, auth_class: type[Any], authentication_module: Any
) -> Iterator[ParityNote]:
    token_base = getattr(authentication_module, "TokenAuthentication", None)
    session_base = getattr(authentication_module, "SessionAuthentication", None)
    basic_base = getattr(authentication_module, "BasicAuthentication", None)
    location = _location(auth_class, context.root)
    literals: list[str] = []
    for klass in inspect.getmro(auth_class):
        if klass is object or klass is authentication_module.BaseAuthentication:
            continue
        for name in ("authenticate", "authenticate_credentials", "authenticate_header"):
            member = vars(klass).get(name)
            if member is not None:
                for text in _sentences(member):
                    if text not in literals:
                        literals.append(text)
    if token_base is not None and issubclass(auth_class, token_base):
        keyword = str(getattr(auth_class, "keyword", "Token"))
        table = _token_table(auth_class)
        yield _note(
            FAMILY_AUTH,
            "SANKA_DRF_PARITY_TOKEN_HEADER",
            f"{_qualified(auth_class)} reads `Authorization: {keyword} <key>` (keyword "
            "compared case-insensitively; an absent or differently keyed header means "
            "anonymous). Failures answer 401 with WWW-Authenticate: "
            f"{keyword} and one of these exact details: "
            f"{_join(_quote(text) for text in literals) or 'see DRF TokenAuthentication'}."
            + (f" Tokens are looked up in {table}." if table else ""),
            location,
        )
    elif session_base is not None and issubclass(auth_class, session_base):
        settings = importlib.import_module("django.conf").settings
        csrf = importlib.import_module("django.middleware.csrf")
        reasons = [
            text
            for name in sorted(dir(csrf))
            if name.startswith("REASON_")
            and isinstance(text := getattr(csrf, name), str)
            and text.endswith(".")
            and "%s" not in text
        ]
        cookie = str(getattr(settings, "SESSION_COOKIE_NAME", "sessionid"))
        yield _note(
            FAMILY_AUTH,
            "SANKA_DRF_PARITY_SESSION_CSRF",
            f"{_qualified(auth_class)} authenticates the `{cookie}` session cookie and then "
            "enforces CSRF on unsafe methods for session users only: failure answers 403 "
            '{"detail": "CSRF Failed: <reason>"} where <reason> is Django\'s text '
            f"({_join(_quote(text) for text in reasons)}). Anonymous requests skip the CSRF "
            "check. It sends no WWW-Authenticate header, so when it is the first "
            "authenticator unauthenticated requests get 403, not 401.",
            location,
        )
    elif basic_base is not None and issubclass(auth_class, basic_base):
        realm = str(getattr(auth_class, "www_authenticate_realm", "api"))
        yield _note(
            FAMILY_AUTH,
            "SANKA_DRF_PARITY_BASIC_HEADER",
            f"{_qualified(auth_class)} reads `Authorization: Basic <base64 user:pass>`; "
            f'failures answer 401 with WWW-Authenticate: Basic realm="{realm}" and one of '
            f"{_join(_quote(text) for text in literals) or 'DRF BasicAuthentication details'}.",
            location,
        )
    else:
        header = _authenticate_header(auth_class)
        yield _note(
            FAMILY_AUTH,
            "SANKA_DRF_PARITY_AUTH_CUSTOM",
            f"{_qualified(auth_class)} is a custom authenticator; authenticate_header() "
            f"returns {_quote(header) if header and header != 'unknown' else header or 'nothing'}"
            + (
                f"; exact failure details in its source: {_join(_quote(text) for text in literals)}"
                if literals
                else ""
            )
            + ".",
            location,
        )


def _token_table(auth_class: type[Any]) -> str | None:
    model = getattr(auth_class, "model", None)
    if model is None:
        try:
            model = auth_class().get_model()
        except Exception:
            return None
    meta = getattr(model, "_meta", None)
    if meta is None:
        return None
    try:
        user_field = next(
            field.column
            for field in meta.fields
            if field.is_relation and field.related_model is not None
        )
    except StopIteration:
        user_field = None
    text = f"table `{meta.db_table}` by primary key column `{meta.pk.column}`"
    if user_field:
        text += f" (user via `{user_field}`; the user must have is_active=True)"
    return text


# --- conditional responses ----------------------------------------------------


def _conditional_notes(context: _Context) -> Iterator[ParityNote]:
    cls = context.view_class
    hits: list[tuple[str, Any, list[str]]] = []
    for klass in inspect.getmro(cls):
        if not _is_project_code(klass, context.root):
            continue
        for name, member in vars(klass).items():
            if not inspect.isfunction(member):
                continue
            source = _source(member)
            found = [token for token in _CONDITIONAL_TOKENS if token in source]
            if found:
                hits.append((name, member, found))
    if not hits:
        return
    symbols = sorted({token for _name, _member, found in hits for token in found})
    headers = sorted(
        {
            match
            for _name, member, _found in hits
            for match in _HEADER_LITERAL.findall(_source(member))
        }
    )
    # Scope the note to the operations that implement or call the logic; hooks that
    # wrap every request (finalize_response, initial, dispatch) keep it on all routes.
    helper_names = {name for name, _member, _found in hits}
    operations = {name for name in helper_names if name in _VIEW_OVERRIDES}
    for klass in inspect.getmro(cls):
        if not _is_project_code(klass, context.root):
            continue
        for name, member in vars(klass).items():
            if name in _VIEW_OVERRIDES and inspect.isfunction(member):
                source = _source(member)
                if any(f"self.{helper}(" in source for helper in helper_names):
                    operations.add(name)
    if "update" in operations:
        operations.add("partial_update")
    request_wide = {"finalize_response", "initial", "handle_exception", "dispatch"}
    if operations and not operations & request_wide and context.operation not in operations:
        return
    methods = _join(name for name, _member, _found in hits)
    django_decorators = any(token in symbols for token in ("condition(", "@etag", "@last_modified"))
    message = (
        f"Conditional-response logic lives in {methods} (symbols: {_join(symbols)}"
        + (f"; headers set: {_join(headers)}" if headers else "")
        + "). Reproduce the exact validator derivation, the 304 response (empty body, the "
        "same validator headers) and any 412 branch, and make the validator change after "
        "every mutation of the represented object."
    )
    if django_decorators:
        message += (
            " Django's condition/etag/last_modified decorators answer 304 to GET/HEAD "
            "whose If-None-Match (or If-Modified-Since) matches, 412 to other methods "
            "whose If-Match fails, and add the ETag/Last-Modified headers to 200 replies."
        )
    yield _note(
        FAMILY_CONDITIONAL,
        "SANKA_DRF_PARITY_CONDITIONAL",
        message,
        _location(hits[0][1], context.root),
    )


# --- pagination, ordering, filtering -----------------------------------------


def _pagination_notes(context: _Context) -> Iterator[ParityNote]:
    if not context.is_collection or context.method != "GET":
        return
    paginator_class = getattr(context.view_class, "pagination_class", None)
    if paginator_class is None:
        return
    pagination = importlib.import_module("rest_framework.pagination")
    paginator = paginator_class()
    location = _location(paginator_class, context.root)
    try:
        schema = paginator.get_paginated_response_schema({"type": "array"})
        envelope = list(schema.get("properties", {}).keys())
    except Exception:
        envelope = []
    envelope_text = (
        "{" + _join(_quote(key) for key in envelope) + "}" if envelope else "its envelope"
    )
    page_size = getattr(paginator, "page_size", None)
    common = (
        f"{_qualified(paginator_class)} wraps the list as {envelope_text} with page_size "
        f"{page_size}; next/previous are absolute URLs built from the request "
        "(scheme://host + path) that keep every other query parameter (search, ordering)"
    )
    if isinstance(paginator, pagination.CursorPagination):
        ordering = getattr(paginator, "ordering", None)
        ordering_text = (
            _join(str(item) for item in ordering)
            if isinstance(ordering, list | tuple)
            else str(ordering)
        )
        yield _note(
            FAMILY_PAGINATION,
            "SANKA_DRF_PARITY_CURSOR_PAGINATION",
            common + f". Cursor pages use query parameter `{paginator.cursor_query_param}`, order "
            f"by ({ordering_text}) and encode the ordering value plus an offset for ties, "
            "so a page stays stable when rows are inserted before the cursor; the first "
            "page has previous=null and the last has next=null. An unreadable cursor answers "
            f'404 {{"detail": {_quote(str(paginator.invalid_cursor_message))}}}.'
            + (
                f" Clients may request up to {paginator.max_page_size} rows via "
                f"`{paginator.page_size_query_param}`."
                if getattr(paginator, "page_size_query_param", None)
                else ""
            ),
            location,
        )
    elif isinstance(paginator, pagination.LimitOffsetPagination):
        yield _note(
            FAMILY_PAGINATION,
            "SANKA_DRF_PARITY_LIMIT_OFFSET_PAGINATION",
            common + f". Parameters `{paginator.limit_query_param}` (default "
            f"{paginator.default_limit}, max {paginator.max_limit}) and "
            f"`{paginator.offset_query_param}`; non-numeric or negative values fall back to "
            "the defaults; count is the unfiltered total after filtering backends ran.",
            location,
        )
    elif isinstance(paginator, pagination.PageNumberPagination):
        yield _note(
            FAMILY_PAGINATION,
            "SANKA_DRF_PARITY_PAGE_NUMBER_PAGINATION",
            common + f". Page parameter `{paginator.page_query_param}` (also "
            f"{_join(_quote(str(item)) for item in paginator.last_page_strings)} for the last "
            "page); an out-of-range or non-numeric page answers 404 "
            f'{{"detail": {_quote(str(paginator.invalid_page_message))}}}.'
            + (
                f" Clients may request up to {paginator.max_page_size} rows via "
                f"`{paginator.page_size_query_param}`."
                if getattr(paginator, "page_size_query_param", None)
                else ""
            ),
            location,
        )
    else:
        yield _note(
            FAMILY_PAGINATION,
            "SANKA_DRF_PARITY_CUSTOM_PAGINATION",
            common + "; the class is custom, so derive its parameters from the source.",
            location,
        )


def _queryset_ordering(context: _Context) -> tuple[str, ...]:
    queryset = getattr(context.view_class, "queryset", None)
    if queryset is None:
        return ()
    explicit = tuple(str(item) for item in (getattr(queryset.query, "order_by", ()) or ()))
    if explicit:
        return explicit
    model = getattr(queryset, "model", None)
    meta = getattr(model, "_meta", None)
    return tuple(str(item) for item in (getattr(meta, "ordering", None) or ()))


def _ordering_notes(context: _Context) -> Iterator[ParityNote]:
    if not context.is_collection or context.method != "GET":
        return
    filters_module = importlib.import_module("rest_framework.filters")
    ordering = _queryset_ordering(context)
    queryset = getattr(context.view_class, "queryset", None)
    model = getattr(queryset, "model", None)
    pk_name = getattr(getattr(model, "_meta", None), "pk", None)
    pk_names = {"pk"} | ({pk_name.name, pk_name.attname} if pk_name is not None else set())
    if queryset is not None:
        if ordering:
            deterministic = any(item.lstrip("-") in pk_names for item in ordering)
            tie = (
                "the primary key is part of the ordering, so the order is total"
                if deterministic
                else "rows equal on these fields fall back to the database's scan order "
                "(SQLite: rowid, i.e. insertion order) — the source has no explicit "
                "tie-break"
            )
            yield _note(
                FAMILY_ORDERING,
                "SANKA_DRF_PARITY_DEFAULT_ORDERING",
                f"Default list order is ({_join(ordering)}) from the queryset or Meta.ordering; "
                f"{tie}.",
            )
        else:
            yield _note(
                FAMILY_ORDERING,
                "SANKA_DRF_PARITY_UNORDERED",
                "The list queryset declares no ordering: rows come back in the database's "
                "scan order (SQLite: rowid, i.e. insertion order).",
            )
    for backend in getattr(context.view_class, "filter_backends", ()):
        if not issubclass(backend, filters_module.OrderingFilter):
            continue
        param = str(getattr(backend, "ordering_param", "ordering"))
        fields = getattr(context.view_class, "ordering_fields", None)
        default = getattr(context.view_class, "ordering", None)
        fields_text = (
            "every serializer field"
            if fields == "__all__"
            else _join(str(item) for item in fields)
            if fields
            else "the serializer's fields (ordering_fields unset)"
        )
        overrides = [name for name, member in vars(backend).items() if inspect.isfunction(member)]
        fallback = (
            "(" + _join(str(item) for item in default) + ")" if default else "the default order"
        )
        yield _note(
            FAMILY_ORDERING,
            "SANKA_DRF_PARITY_ORDERING_FILTER",
            f"`?{param}=` accepts comma-separated names from {fields_text} with a leading "
            "`-` for descending; unknown names are dropped silently and an empty result "
            f"falls back to {fallback}."
            + (
                f" {_qualified(backend)} overrides {_join(overrides)}: read it for the "
                "exact tie-break it appends."
                if overrides and _is_project_code(backend, context.root)
                else ""
            ),
            _location(backend, context.root),
        )


def _filtering_notes(context: _Context) -> Iterator[ParityNote]:
    if not context.is_collection or context.method != "GET":
        return
    filters_module = importlib.import_module("rest_framework.filters")
    for backend in getattr(context.view_class, "filter_backends", ()):
        if issubclass(backend, filters_module.OrderingFilter):
            continue
        location = _location(backend, context.root)
        if issubclass(backend, filters_module.SearchFilter):
            param = str(getattr(backend, "search_param", "search"))
            fields = tuple(str(item) for item in getattr(context.view_class, "search_fields", ()))
            yield _note(
                FAMILY_FILTERING,
                "SANKA_DRF_PARITY_SEARCH_FILTER",
                f"`?{param}=` splits on whitespace and commas (quoted phrases stay whole); "
                "every term must match at least one of "
                f"({_join(fields) or 'no search_fields'}) — no prefix means icontains, "
                "`^` istartswith, `=` iexact, `@` full-text search, `$` iregex; results are "
                "distinct and keep the list ordering, and pagination links keep the "
                "parameter.",
                location,
            )
            continue
        overrides = [name for name, member in vars(backend).items() if inspect.isfunction(member)]
        yield _note(
            FAMILY_FILTERING,
            "SANKA_DRF_PARITY_FILTER_BACKEND",
            f"{_qualified(backend)} filters the list"
            + (f" (defines {_join(overrides)})" if overrides else "")
            + "; reproduce its query parameters and semantics from the source.",
            location,
        )


# --- multipart and files -------------------------------------------------------


def _multipart_notes(context: _Context) -> Iterator[ParityNote]:
    if not context.writes:
        return
    fields_module = importlib.import_module("rest_framework.fields")
    parsers_module = importlib.import_module("rest_framework.parsers")
    view = _view_instance(context)
    parsers = list(getattr(context.view_class, "parser_classes", ()))
    if parsers and parsers_module.JSONParser not in parsers:
        media = [str(item.media_type) for item in parsers]
        yield _note(
            FAMILY_MULTIPART,
            "SANKA_DRF_PARITY_PARSERS",
            f"Only {_join(media)} request bodies are parsed here; any other Content-Type "
            '(including application/json) answers 415 {"detail": '
            f"{_quote(_default_detail('UnsupportedMediaType', media_type='<type>'))}}} "
            "(<type> = the request's media type).",
        )
    serializer_class = _serializer_class(context, view)
    fields = _serializer_fields(serializer_class)
    file_fields = [
        (label, field)
        for label, field, container in _walk_fields(fields)
        if container is None
        and isinstance(field, fields_module.FileField)
        and not getattr(field, "read_only", False)
    ]
    if not file_fields:
        return
    for label, field in file_fields:
        messages = {
            key: text
            for key in ("required", "invalid", "no_name", "empty", "max_length")
            if (text := _format_message(field, key))
        }
        rendered = _join(f"{key}: {_quote(text)}" for key, text in messages.items())
        representation = (
            "absolute URL (request host + MEDIA_URL + stored name)"
            if getattr(field, "use_url", True)
            else "the stored name"
        )
        validator = None
        if serializer_class is not None:
            validator = getattr(serializer_class, f"validate_{label.split('.')[-1]}", None)
        custom = _sentences(validator) if validator is not None else ()
        yield _note(
            FAMILY_MULTIPART,
            "SANKA_DRF_PARITY_FILE_FIELD",
            f"`{label}` is a {type(field).__name__} (required={getattr(field, 'required', False)}, "
            f"allow_empty_file={getattr(field, 'allow_empty_file', False)}, "
            f"write_only={getattr(field, 'write_only', False)}); exact 400 details by case — "
            f"{rendered}"
            + (
                f"; validate_{label.split('.')[-1]} adds {_join(_quote(text) for text in custom)}"
                if custom
                else ""
            )
            + f". Its representation is {representation}. Uploaded bytes are read from "
            "Django's MultiPartParser: part content is byte-exact even when it contains "
            "boundary-like text, and the filename is the basename of Content-Disposition.",
            _location(validator, context.root) if validator is not None else None,
        )


# --- uniqueness, nullability, exact messages ----------------------------------


def _uniqueness_notes(context: _Context) -> Iterator[ParityNote]:
    if not context.writes:
        return
    validators_module = importlib.import_module("rest_framework.validators")
    serializers_module = importlib.import_module("rest_framework.serializers")
    view = _view_instance(context)
    serializer_class = _serializer_class(context, view)
    if serializer_class is None:
        return
    try:
        serializer = serializer_class()
    except Exception:
        return
    fields = _bound_fields(serializer)
    for label, field, container in _walk_fields(fields):
        if container is not None or getattr(field, "read_only", False):
            continue
        for validator in getattr(field, "validators", ()):
            if isinstance(validator, validators_module.UniqueValidator):
                yield _note(
                    FAMILY_UNIQUENESS,
                    "SANKA_DRF_PARITY_UNIQUE_FIELD",
                    f"A duplicate `{label}` is rejected before the database is touched: 400 "
                    f"{{{_quote(label.split('.')[-1])}: [{_quote(str(validator.message))}]}}; "
                    "on update the current instance is excluded from the check.",
                )
    for label, target in [("", serializer)] + [
        (
            f"{label}[]." if container == "list" else f"{label}.",
            field.child if container == "list" else field,
        )
        for label, field, container in _walk_fields(fields)
        if container is not None
        and isinstance(
            field.child if container == "list" else field, serializers_module.BaseSerializer
        )
    ]:
        for validator in getattr(target, "validators", ()):
            if isinstance(validator, validators_module.UniqueTogetherValidator):
                names = tuple(str(item) for item in validator.fields)
                try:
                    text = str(validator.message).format(field_names=", ".join(names))
                except (IndexError, KeyError):
                    text = str(validator.message)
                yield _note(
                    FAMILY_UNIQUENESS,
                    "SANKA_DRF_PARITY_UNIQUE_TOGETHER",
                    f"{label or 'The serializer'} enforces unique_together ({_join(names)}): a "
                    f'duplicate combination answers 400 {{"non_field_errors": '
                    f"[{_quote(text)}]}}; all listed fields must be present for the "
                    "check to run.",
                )


def _nullability_notes(context: _Context) -> Iterator[ParityNote]:
    if not context.writes:
        return
    view = _view_instance(context)
    fields = _serializer_fields(_serializer_class(context, view))
    rejects: list[str] = []
    accepts: list[str] = []
    lists: list[str] = []
    null_message: str | None = None
    for label, field, container in _walk_fields(fields):
        if getattr(field, "read_only", False):
            continue
        if container == "list":
            empty = _format_message(field, "empty")
            not_a_list = _format_message(field, "not_a_list")
            lists.append(
                f"`{label}`: allow_null={getattr(field, 'allow_null', False)}, "
                f"allow_empty={getattr(field, 'allow_empty', True)}"
                + (f", empty → {_quote(empty)}" if empty else "")
                + (f", non-list → {_quote(not_a_list)}" if not_a_list else "")
            )
            continue
        if getattr(field, "allow_null", False):
            accepts.append(label)
        else:
            rejects.append(label)
            null_message = null_message or _format_message(field, "null")
    if not (rejects or accepts or lists):
        return
    parts: list[str] = []
    if rejects:
        parts.append(
            f"null is rejected for {_join(f'`{item}`' for item in rejects)}"
            + (f" with 400 {{<field>: [{_quote(null_message)}]}}" if null_message else "")
        )
    if accepts:
        parts.append(
            f"null is stored and echoed back as null for {_join(f'`{item}`' for item in accepts)}"
        )
    if lists:
        parts.append("nested lists: " + "; ".join(lists))
    yield _note(
        FAMILY_NULLABILITY,
        "SANKA_DRF_PARITY_NULLABILITY",
        "; ".join(parts) + ".",
    )


def _messages_notes(context: _Context) -> Iterator[ParityNote]:
    if not context.writes:
        return
    view = _view_instance(context)
    fields = _serializer_fields(_serializer_class(context, view))
    rendered: list[str] = []
    for label, field, container in _walk_fields(fields):
        if getattr(field, "read_only", False) or container == "nested":
            continue
        messages = [
            f"{key}={_quote(text)}"
            for key in _MESSAGE_KEYS
            if key in field.error_messages and (text := _format_message(field, key))
        ]
        if messages:
            rendered.append(f"`{label}` ({type(field).__name__}): {_join(messages)}")
    if not rendered:
        return
    parsers_module = importlib.import_module("rest_framework.parsers")
    parses_json = any(
        issubclass(parser, parsers_module.JSONParser)
        for parser in getattr(context.view_class, "parser_classes", ())
    )
    yield _note(
        FAMILY_MESSAGES,
        "SANKA_DRF_PARITY_FIELD_MESSAGES",
        "Validation answers 400 with one list of strings per failing field, keyed by field "
        "name (nested errors nest by index and name); the exact strings are — "
        + "; ".join(rendered)
        + (
            '. Unparseable JSON answers 400 {"detail": "JSON parse error - <reason>"}.'
            if parses_json
            else "."
        ),
    )


# --- project overrides ---------------------------------------------------------


def _override_notes(context: _Context) -> Iterator[ParityNote]:
    view_overrides = _own_methods(context.view_class, _VIEW_OVERRIDES, context.root)
    for name, member in view_overrides:
        literals = _sentences(member)
        yield _note(
            FAMILY_OVERRIDES,
            "SANKA_DRF_PARITY_VIEW_OVERRIDE",
            f"{_qualified(context.view_class)}.{name} is overridden in project code"
            + (
                f"; exact strings it emits: {_join(_quote(text) for text in literals)}"
                if literals
                else ""
            )
            + ".",
            _location(member, context.root),
        )
    if context.actions is not None:
        extra = getattr(context.view_class, context.operation, None)
        if extra is not None and getattr(extra, "mapping", None) is not None:
            detail = getattr(extra, "detail", None)
            literals = _sentences(extra)
            yield _note(
                FAMILY_OVERRIDES,
                "SANKA_DRF_PARITY_CUSTOM_ACTION",
                f"`{context.operation}` is a custom @action (detail={detail}); its response "
                "shape and status come only from the source"
                + (
                    f"; exact strings it emits: {_join(_quote(text) for text in literals)}"
                    if literals
                    else ""
                )
                + ".",
                _location(extra, context.root),
            )
    view = _view_instance(context)
    serializer_class = _serializer_class(context, view)
    if serializer_class is None:
        return
    seen: set[type[Any]] = set()
    stack: list[tuple[str, type[Any]]] = [("", serializer_class)]
    serializers_module = importlib.import_module("rest_framework.serializers")
    while stack:
        prefix, klass = stack.pop(0)
        if klass in seen:
            continue
        seen.add(klass)
        names = tuple(_SERIALIZER_OVERRIDES) + tuple(
            name for name in vars(klass) if name.startswith("validate_")
        )
        for name, member in _own_methods(klass, names, context.root):
            literals = _sentences(member)
            yield _note(
                FAMILY_OVERRIDES,
                "SANKA_DRF_PARITY_SERIALIZER_OVERRIDE",
                f"{prefix}{_qualified(klass)}.{name} is overridden in project code"
                + (
                    f"; exact strings it emits: {_join(_quote(text) for text in literals)}"
                    if literals
                    else ""
                )
                + ".",
                _location(member, context.root),
            )
        for field_name, field in _serializer_fields(klass):
            child = getattr(field, "child", field)
            if isinstance(child, serializers_module.BaseSerializer):
                stack.append((f"{prefix}{field_name}.", type(child)))


__all__ = ["FAMILIES", "route_parity_notes"]

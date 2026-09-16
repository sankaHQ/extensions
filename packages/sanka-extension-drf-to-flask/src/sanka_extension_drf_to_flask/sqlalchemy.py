# SPDX-License-Identifier: Apache-2.0
"""Qualify captured DRF contracts and emit a standalone synchronous Flask target."""

from __future__ import annotations

import importlib
import inspect
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sanka_code_migration.drf.model import FrameworkScan, SerializerIR
from sanka_code_migration.drf.scan import _to_fastapi_path

from . import native_runtime
from .model_runtime import ModelInput
from .sqlalchemy_access import qualify_access
from .sqlalchemy_carryover import capture_conditional, capture_response_overrides

_KINDS = {
    "char",
    "integer",
    "big_integer",
    "boolean",
    "uuid",
    "decimal",
    "datetime",
    "choice",
    "related_pk",
    "related_uuid",
}
_OPERATIONS = {"list", "create", "retrieve", "update", "partial_update", "destroy"}


def capture_middleware() -> dict[str, Any]:
    # Called only by the source capture worker, never by the generated application.
    settings = importlib.import_module("django.conf").settings
    RequestFactory = importlib.import_module("django.test").RequestFactory
    get_resolver = importlib.import_module("django.urls").get_resolver
    bad_request = importlib.import_module("django.views.defaults").bad_request

    supported = {
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.middleware.common.CommonMiddleware",
        "django.middleware.csrf.CsrfViewMiddleware",
        "django.middleware.security.SecurityMiddleware",
        "django.middleware.clickjacking.XFrameOptionsMiddleware",
    }
    gaps = ["middleware:" + value for value in settings.MIDDLEWARE if value not in supported]
    if len(settings.MIDDLEWARE) != len(set(settings.MIDDLEWARE)):
        gaps.append("duplicate-middleware")
    for setting in (
        "SECURE_PROXY_SSL_HEADER",
        "USE_X_FORWARDED_HOST",
        "USE_X_FORWARDED_PORT",
        "PREPEND_WWW",
        "DISALLOWED_USER_AGENTS",
    ):
        if getattr(settings, setting, None):
            gaps.append("middleware-setting:" + setting)
    if settings.DEBUG:
        gaps.append("debug-error-responses")
    if get_resolver().resolve_error_handler(400) is not bad_request:
        gaps.append("custom-handler400")
    response = bad_request(RequestFactory().get("/"), Exception())
    referrer = settings.SECURE_REFERRER_POLICY
    return {
        "order": list(settings.MIDDLEWARE),
        "gaps": gaps,
        "redirect_host": settings.SECURE_SSL_HOST,
        "redirect_exempt": list(settings.SECURE_REDIRECT_EXEMPT),
        "referrer_policy": ",".join(
            [v.strip() for v in referrer.split(",")] if isinstance(referrer, str) else referrer
        )
        if referrer
        else None,
        "bad_request": response.content.decode(),
    }


def _capture_request_body_limit() -> int | None:
    """Probe the configured stock DRF JSON request path without invoking a view."""
    settings = importlib.import_module("django.conf").settings
    configured = settings.DATA_UPLOAD_MAX_MEMORY_SIZE
    stock_default = importlib.import_module(
        "django.conf.global_settings"
    ).DATA_UPLOAD_MAX_MEMORY_SIZE
    if configured != stock_default or type(configured) is not int or configured < 16:
        raise ValueError("DATA_UPLOAD_MAX_MEMORY_SIZE is outside the native contract")
    request_factory = importlib.import_module("django.test").RequestFactory()
    drf_request = importlib.import_module("rest_framework.request").Request
    json_parser = importlib.import_module("rest_framework.parsers").JSONParser
    request_too_big = importlib.import_module("django.core.exceptions").RequestDataTooBig

    def parse(size: int) -> None:
        prefix = b'{"padding":"'
        suffix = b'"}'
        payload = prefix + b"x" * (size - len(prefix) - len(suffix)) + suffix
        request = request_factory.generic("POST", "/", payload, content_type="application/json")
        _ = drf_request(request, parsers=[json_parser()]).data

    parse(configured)
    try:
        parse(configured + 1)
    except request_too_big:
        return configured
    return None


def capture_sqlalchemy_overrides(scan: FrameworkScan) -> dict[str, Any]:
    """Capture facts absent from the legacy scan, inside the caller's source worker.

    Defaults are inspected, never called. Unknown values remain blocking gaps.
    """
    fields_module = importlib.import_module("rest_framework.fields")
    api_settings = importlib.import_module("rest_framework.settings").api_settings
    api_views = importlib.import_module("rest_framework.views")
    parsers = importlib.import_module("rest_framework.parsers")
    renderers = importlib.import_module("rest_framework.renderers")
    negotiation = importlib.import_module("rest_framework.negotiation")
    metadata = importlib.import_module("rest_framework.metadata")
    urls = importlib.import_module("django.urls")
    converters = importlib.import_module("django.urls.converters")
    known = {
        converters.IntConverter: "int",
        converters.StringConverter: "str",
        converters.SlugConverter: "str",
        converters.UUIDConverter: "uuid",
        converters.PathConverter: "str",
    }
    result: dict[str, Any] = {
        "serializers": {},
        "patterns": [],
        "gaps": [],
        "object_not_found": {},
        "auth_messages": {},
        "listing": {},
        "session_auth": {},
        "session_serializers": {},
        "middleware": capture_middleware(),
        "request_body_limit": _capture_request_body_limit(),
        "format_query_param": api_settings.URL_FORMAT_OVERRIDE,
    }
    from .sqlalchemy_sessions import capture_session_auth

    for view in scan.view_details:
        if not any(
            route.view == view.name
            and route.authentication == ("rest_framework.authentication.SessionAuthentication",)
            for route in scan.routes
        ):
            continue
        module, _, name = view.name.rpartition(".")
        try:
            view_class = getattr(importlib.import_module(module), name)
            result["session_auth"][view.name] = capture_session_auth(view_class)
            from sanka_code_migration.drf.scan import _serializer_ir

            serializer_class = view_class.serializer_class
            serializer_name = serializer_class.__module__ + "." + serializer_class.__qualname__
            serializer = _serializer_ir(view_class, serializer_name)
            if serializer is None:
                raise ValueError("session serializer is outside the native contract")
            result["session_serializers"][serializer_name] = json.loads(
                json.dumps(asdict(serializer), allow_nan=False)
            )
        except (AttributeError, ImportError, TypeError, ValueError):
            result["gaps"].append({"source": view.name, "feature": "session-authentication"})
    if api_settings.EXCEPTION_HANDLER is not api_views.exception_handler:
        result["gaps"].append({"source": "REST_FRAMEWORK", "feature": "exception-handler"})
    session_middleware = {
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "django.middleware.csrf.CsrfViewMiddleware",
    }
    if (
        session_middleware.intersection(result["middleware"]["order"])
        and not result["session_auth"]
    ):
        result["gaps"].append({"source": "MIDDLEWARE", "feature": "unqualified-session-middleware"})
    if api_settings.NON_FIELD_ERRORS_KEY != "non_field_errors":
        result["gaps"].append({"source": "REST_FRAMEWORK", "feature": "non-field-errors-key"})
    defaults = importlib.import_module("django.views.defaults")
    test = importlib.import_module("django.test")
    settings = importlib.import_module("django.conf").settings
    if (
        urls.get_resolver().resolve_error_handler(404) is not defaults.page_not_found
        or settings.TEMPLATES
    ):
        result["gaps"].append({"source": "handler404", "feature": "custom-not-found-handler"})
    response = defaults.page_not_found(test.RequestFactory().get("/"), Exception("Not found"))
    result["not_found_response"] = {
        "body": response.content.decode(),
        "content_type": response.headers["Content-Type"],
    }
    if settings.DATA_UPLOAD_MAX_MEMORY_SIZE != 2_621_440:
        result["gaps"].append({"source": "settings", "feature": "request-body-limit"})
    if urls.get_resolver().resolve_error_handler(500) is not defaults.server_error:
        result["gaps"].append({"source": "handler500", "feature": "custom-server-error-handler"})
    response = defaults.server_error(test.RequestFactory().get("/"))
    result["server_error_response"] = {
        "body": response.content.decode(),
        "content_type": response.headers["Content-Type"],
    }

    def capture_serializer(serializer: SerializerIR) -> None:
        module, _, name = serializer.name.rpartition(".")
        source = getattr(importlib.import_module(module), name)()
        shortcuts = importlib.import_module("django.shortcuts")
        http = importlib.import_module("django.http")
        try:
            shortcuts.get_object_or_404(source.Meta.model._default_manager.none())
        except http.Http404 as error:
            result["object_not_found"][serializer.name] = str(error)
        captured = {}
        for field_name, field in source.fields.items():
            default = field.default
            facts: dict[str, Any] = {"write_only": bool(field.write_only), "has_default": False}
            if any(f.name == field_name and f.kind == "nested_many" for f in serializer.fields):
                if (
                    type(field)
                    is not importlib.import_module("rest_framework.serializers").ListSerializer
                    or field.min_length is not None
                    or field.max_length is not None
                    or not source.Meta.model._meta.get_field(
                        field_name
                    ).field.target_field.primary_key
                ):
                    result["gaps"].append(
                        {"source": serializer.name, "feature": "nested-list-contract"}
                    )
                validation_error = importlib.import_module(
                    "rest_framework.exceptions"
                ).ValidationError
                try:
                    field.run_validation(["not-an-object"])
                except validation_error as error:
                    facts["indexed_errors"] = isinstance(error.detail, dict)
                try:
                    field.child.run_validation(None)
                except validation_error as error:
                    facts["child_null_error"] = error.detail
            captured_field = next((f for f in serializer.fields if f.name == field_name), None)
            if field.source != field_name and not (
                captured_field
                and captured_field.kind == "related_preview"
                and captured_field.supported
            ):
                result["gaps"].append({"source": serializer.name, "feature": "serializer-source"})
            if default is not fields_module.empty:
                if default is None or type(default) in (str, int, float, bool):
                    facts.update(has_default=True, default=default)
                else:
                    result["gaps"].append(
                        {"source": serializer.name, "feature": "serializer-default"}
                    )
            captured[field_name] = facts
        result["serializers"][serializer.name] = captured
        for field in serializer.fields:
            if field.child:
                capture_serializer(field.child)

    for serializer in scan.serializer_details:
        capture_serializer(serializer)
    for value in result["session_serializers"].values():
        capture_serializer(SerializerIR.from_dict(value))

    def walk(
        patterns: Any,
        prefix: str = "",
        regex_prefix: str = "",
        inherited: dict[str, str] | None = None,
    ) -> None:
        for pattern in patterns:
            raw = prefix + str(pattern.pattern)
            regex = regex_prefix + pattern.pattern.regex.pattern.removeprefix("^")
            kinds = dict(inherited or {})
            for name, converter in getattr(pattern.pattern, "converters", {}).items():
                kind = known.get(type(converter))
                if kind is None and type(converter).__module__ == "rest_framework.urlpatterns":
                    kind = "format"
                if kind is None:
                    result["gaps"].append({"source": raw, "feature": "route-converter"})
                else:
                    kinds[name] = kind
            nested = getattr(pattern, "url_patterns", None)
            if nested is not None:
                walk(nested, raw, regex, kinds)
                continue
            callback = getattr(pattern, "callback", None)
            cls = getattr(callback, "cls", None)
            if cls is None:
                continue
            hooks = (
                "dispatch",
                "initial",
                "initialize_request",
                "finalize_response",
                "get_authenticators",
                "get_permissions",
                "get_throttles",
                "get_parsers",
                "get_renderers",
                "perform_authentication",
                "check_permissions",
                "check_object_permissions",
                "check_throttles",
                "handle_exception",
                "get_exception_handler",
                "get_content_negotiator",
                "perform_content_negotiation",
                "determine_version",
                "permission_denied",
                "throttled",
                "http_method_not_allowed",
                "__init__",
                "options",
            )
            viewsets = importlib.import_module("rest_framework.viewsets")
            base = (
                viewsets.ModelViewSet
                if issubclass(cls, viewsets.ModelViewSet)
                else api_views.APIView
            )
            if any(getattr(cls, name) is not getattr(base, name) for name in hooks):
                result["gaps"].append({"source": raw, "feature": "view-request-hooks"})
            if (
                list(cls.parser_classes) != [parsers.JSONParser]
                or list(cls.renderer_classes) != [renderers.JSONRenderer]
                or cls.content_negotiation_class is not negotiation.DefaultContentNegotiation
                or cls.metadata_class is not metadata.SimpleMetadata
                or cls.versioning_class is not None
                or cls.throttle_classes
            ):
                result["gaps"].append({"source": raw, "feature": "view-http-policies"})
            view_name = f"{cls.__module__}.{cls.__qualname__}"
            captured_view = next((v for v in scan.view_details if v.name == view_name), None)
            if captured_view:
                import builtins

                if captured_view.carryover:
                    source_class: type[Any] = cls
                    mixins = importlib.import_module("rest_framework.mixins")
                    stock_methods = {
                        "create": mixins.CreateModelMixin.create,
                        "list": mixins.ListModelMixin.list,
                        "retrieve": mixins.RetrieveModelMixin.retrieve,
                        "update": mixins.UpdateModelMixin.update,
                        "partial_update": mixins.UpdateModelMixin.partial_update,
                        "destroy": mixins.DestroyModelMixin.destroy,
                    }
                    carried_names = {
                        method["name"] for method in captured_view.carryover["methods"]
                    }
                    for name in carried_names & stock_methods.keys():
                        function = getattr(source_class, name)
                        closure = dict(
                            zip(
                                function.__code__.co_freevars,
                                (cell.cell_contents for cell in function.__closure__ or ()),
                                strict=True,
                            )
                        )
                        next_method = next(
                            (
                                vars(base)[name]
                                for base in source_class.__mro__[1:]
                                if name in vars(base)
                            ),
                            None,
                        )
                        if (
                            closure.get("__class__") is not source_class
                            or next_method is not stock_methods[name]
                        ):
                            result["gaps"].append({"source": raw, "feature": "custom-super-chain"})
                    # Stock partial_update also dispatches through inherited self.update methods.
                    if carried_names & {"update", "partial_update"}:
                        for name in {"update", "partial_update"} - carried_names:
                            if getattr(source_class, name) is not stock_methods[name]:
                                result["gaps"].append(
                                    {"source": raw, "feature": "custom-super-chain"}
                                )
                    for method in captured_view.carryover["methods"]:
                        function = getattr(cls, method["name"])
                        if (
                            function.__globals__.get("super", builtins.super) is not builtins.super
                            or function.__builtins__.get("super") is not builtins.super
                            or "super" in function.__code__.co_freevars
                        ):
                            result["gaps"].append({"source": raw, "feature": "shadowed-super"})
                from .sqlalchemy_listing import capture_listing

                try:
                    result["listing"][view_name] = (
                        {}
                        if captured_view.access.get("member_action")
                        else capture_listing(cls, captured_view.listing)
                    )
                except ValueError:
                    result["gaps"].append({"source": raw, "feature": "listing"})
            if captured_view and captured_view.auth and captured_view.auth.token_keyword:
                from sanka_code_migration.drf.access_contracts import plain_model

                authenticator = cls.authentication_classes[0]()
                if not plain_model(authenticator.get_model()):
                    result["gaps"].append({"source": raw, "feature": "token-model"})
                exceptions = importlib.import_module("rest_framework.exceptions")
                translation = importlib.import_module("django.utils.translation")
                auth_messages = dict(captured_view.auth.messages)
                if captured_view.auth.owner_attname:
                    permissions = importlib.import_module("rest_framework.permissions")
                    owner = next(
                        p for p in cls.permission_classes if p is not permissions.IsAuthenticated
                    )
                    if hasattr(owner, "message"):
                        if isinstance(owner.message, (dict, list)):
                            result["gaps"].append(
                                {"source": raw, "feature": "permission-error-shape"}
                            )
                        else:
                            auth_messages["forbidden"] = str(owner.message)
                auth_messages["invalid_token"] = translation.gettext("Invalid token.")
                auth_messages["inactive_user"] = translation.gettext("User inactive or deleted.")
                from types import SimpleNamespace

                try:
                    authenticator.authenticate(
                        SimpleNamespace(
                            META={"HTTP_AUTHORIZATION": authenticator.keyword + " \xff"},
                            headers={"authorization": authenticator.keyword + " \xff"},
                        )
                    )
                except exceptions.AuthenticationFailed as error:
                    auth_messages["invalid_characters"] = str(error.detail)
                result["auth_messages"][view_name] = auth_messages
            path, supported = _to_fastapi_path(raw)
            if not supported or pattern.pattern.regex.groups != len(
                pattern.pattern.regex.groupindex
            ):
                result["gaps"].append({"source": raw, "feature": "route-pattern"})
            if getattr(pattern, "default_args", {}):
                result["gaps"].append({"source": raw, "feature": "route-default-arguments"})
            result["patterns"].append(
                {
                    "regex": regex,
                    "path": path,
                    "converters": kinds,
                    "view": f"{cls.__module__}.{cls.__qualname__}",
                    "append_slash": getattr(callback, "should_append_slash", True),
                }
            )

    walk(urls.get_resolver().url_patterns)
    return result


def _serializer_gaps(source: SerializerIR) -> list[str]:
    gaps = []
    nested = [f for f in source.fields if f.kind == "nested_many" and not f.read_only]
    nested_create = bool(
        source.create_contract
        and source.create_contract.get("style") == "nested"
        and source.create_contract.get("atomic") is True
        and len(nested) == 1
    )
    parent_create = bool(
        source.create_contract and source.create_contract.get("style") == "parent_duplicate"
    )
    if not source.supported or (
        source.create_style != "default" and not nested_create and not parent_create
    ):
        gaps.append("serializer-writes")
    for field in source.fields:
        if field.kind == "nested_many" and field.child and field.supported:
            gaps.extend(_serializer_gaps(field.child))
        elif (
            field.kind in {"membership_read", "related_preview"}
            and field.read_only
            and field.supported
        ):
            pass
        elif not field.supported or field.kind not in _KINDS or field.child or field.relation:
            gaps.append("serializer-field:" + field.name)
        if field.kind in {"related_pk", "related_uuid"} and not field.read_only:
            gaps.append("writable-related-field:" + field.name)
        if field.kind == "datetime" and field.timezone:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            try:
                ZoneInfo(field.timezone)
            except ZoneInfoNotFoundError:
                gaps.append("datetime-timezone:" + field.name)
    return gaps


def qualify_routes(
    scan: FrameworkScan, overrides: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Flask's own qualification; legacy FastAPI native flags are not reused."""
    captured = overrides or {}
    serializers = {s.name: s for s in scan.serializer_details}
    serializers.update(
        {
            name: SerializerIR.from_dict(value)
            for name, value in captured.get("session_serializers", {}).items()
        }
    )
    views = {v.name: v for v in scan.view_details}
    roots = {r.path for r in scan.api_roots}
    rows = []
    for route in scan.routes:
        gaps = []
        if not overrides:
            gaps.append("flask-source-overrides-not-captured")
        gaps.extend(g["feature"] for g in captured.get("gaps", []))
        gaps.extend(captured.get("middleware", {}).get("gaps", []))
        if "middleware" not in captured:
            gaps.append("middleware-not-captured")
        if scan.skipped_routes:
            gaps.append("non-drf-routes")
        session = bool(
            route.view in captured.get("session_auth", {})
            and route.authentication == ("rest_framework.authentication.SessionAuthentication",)
            and route.permissions == ("rest_framework.permissions.IsAuthenticated",)
        )
        allowed_legacy_reasons = {
            "SANKA_DRF_AUTH_PERMISSIONS_UNSUPPORTED",
            "SANKA_DRF_MIDDLEWARE_UNSUPPORTED",
        }
        if not route.supported and not (
            session
            and route.adaptation_reasons
            and all(reason.code in allowed_legacy_reasons for reason in route.adaptation_reasons)
        ):
            gaps.append("route-pattern")
        view = views.get(route.view)
        token = bool(
            view
            and view.auth
            and view.auth.token_keyword
            and (view.auth.require_authenticated or view.access.get("permission"))
            and route.authentication == ("rest_framework.authentication.TokenAuthentication",)
        )
        if route.authentication and not (token or session):
            gaps.append("authentication")
        if not (token or session) and any(
            p != "rest_framework.permissions.AllowAny" for p in route.permissions
        ):
            gaps.append("permissions")
        metadata = route.options.get("anonymous", {})
        if metadata.get("renders") != ["application/json"]:
            gaps.append("content-negotiation")
        if metadata.get("parses") != ["application/json"]:
            gaps.append("request-parsers")
        gaps.extend(
            reason.code
            for reason in route.adaptation_reasons
            if reason.code != "SANKA_DRF_MIDDLEWARE_UNSUPPORTED"
            and not (session and reason.code == "SANKA_DRF_AUTH_PERMISSIONS_UNSUPPORTED")
            and not (
                route.view in captured.get("listing", {})
                and reason.code
                in {
                    "SANKA_DRF_PAGINATION_UNSUPPORTED",
                    "SANKA_DRF_FILTER_BACKENDS_UNSUPPORTED",
                    "SANKA_DRF_SEARCH_FIELDS_UNSUPPORTED",
                    "SANKA_DRF_ORDERING_FILTER_UNSUPPORTED",
                }
            )
        )
        source = serializers.get(route.serializer or "")
        view = views.get(route.view)
        root_path = route.path.replace("<drf_format_suffix:format>", "")
        is_root = root_path in roots or route.path in roots
        if captured.get("session_auth") and not session and not is_root:
            gaps.append("mixed-session-authentication")
        if is_root:
            pass
        elif (
            source is None
            or view is None
            or (
                route.operation not in _OPERATIONS
                and not (route.operation == "put" and view.access.get("member_action"))
            )
        ):
            gaps.append("view-contract")
        else:
            gaps.extend(_serializer_gaps(source))
            if source.name not in captured.get("serializers", {}):
                gaps.append("serializer-defaults-not-captured")
            try:
                qualify_access(view.access, asdict(source))
            except (ValueError, TypeError, KeyError):
                gaps.append("access")
            if (
                view.carryover
                and capture_conditional(view.carryover) is None
                and capture_response_overrides(view.carryover) is None
            ):
                gaps.append("carryover")
            if view.name not in captured.get("listing", {}):
                gaps.append("listing-not-captured")
            if (
                not (token or session)
                and view.auth
                and (
                    view.auth.require_authenticated
                    or view.auth.token_keyword
                    or view.auth.owner_field
                    or view.auth.inject_owner
                )
            ):
                gaps.append("authentication")
        if not any(
            p["path"] == route.path and p["view"] == route.view
            for p in captured.get("patterns", [])
        ):
            gaps.append("route-pattern-not-captured")
        rows.append(
            {
                "method": route.method,
                "path": route.path,
                "view": route.view,
                "operation": route.operation,
                "native": not gaps,
                "gaps": sorted(set(gaps)),
            }
        )
    return rows


def render_sqlalchemy(
    scan: FrameworkScan,
    schema: dict[str, Any],
    *,
    overrides: dict[str, Any] | None = None,
    module_prefix: str = "",
) -> dict[str, str]:
    if module_prefix and not module_prefix.isidentifier():
        raise ValueError("module_prefix must be a Python package identifier")
    imports = module_prefix + "." if module_prefix else ""
    rows = qualify_routes(scan, overrides)
    if schema["gaps"] or any(not row["native"] for row in rows):
        raise ValueError("native Flask generation has unresolved source or schema gaps")
    assert overrides is not None
    tables = {t["name"]: t for t in schema["tables"]}
    resources = {}

    def build_resource(serializer: SerializerIR) -> dict[str, Any]:
        resource = asdict(serializer)
        resource.pop("create_source", None)
        resource.pop("create_imports", None)
        columns = tables[serializer.db_table]["columns"]
        by_attribute = {c["attribute"]: c for c in columns}
        resource["columns"] = columns
        resource["not_found"] = overrides["object_not_found"][serializer.name]
        resource["pk_column"] = by_attribute[serializer.pk_attname]["name"]
        resource["lookup_column"] = by_attribute[
            serializer.pk_attname if serializer.lookup == "pk" else serializer.lookup
        ]["name"]
        for field in resource["fields"]:
            facts = overrides["serializers"][serializer.name].get(field["name"])
            if (
                facts is None
                and field["read_only"]
                and any(
                    view.access.get("member_action")
                    and view.access.get("serializer") == serializer.name
                    for view in scan.view_details
                )
            ):
                facts = {"has_default": False, "write_only": False}
            if facts is None:
                raise ValueError("serializer field override was not captured: " + field["name"])
            field.update(facts)
            field["storage_timezone"] = schema.get("timezone", "UTC")
            field["messages"] = dict(field["messages"])
            if field["kind"] in {"membership_read", "related_preview"}:
                field["column"] = None
            elif field["kind"] == "nested_many":
                child = next(f.child for f in serializer.fields if f.name == field["name"])
                assert child is not None
                field["child"] = build_resource(child)
                field["column"] = field["name"]
                field["foreign_key"] = next(
                    c["name"]
                    for c in field["child"]["columns"]
                    if c["attribute"] == field["attname"]
                )
            else:
                field["column"] = by_attribute[field["attname"] or field["name"]]["name"]
            # The existing pure Flask validator uses DRF class names.
            field["scalar_kind"] = {
                "char": "CharField",
                "integer": "IntegerField",
                "big_integer": "IntegerField",
                "choice": "ChoiceField",
                "decimal": "DecimalField",
            }.get(field["kind"])
            field["max_whole_digits"] = (
                field["max_digits"] - field["decimal_places"]
                if field["max_digits"] is not None
                else None
            )
        resource["ordering"] = [
            ("-" if name.startswith("-") else "")
            + by_attribute[name.lstrip("-") if name.lstrip("-") != "pk" else serializer.pk_attname][
                "name"
            ]
            for name in serializer.ordering
        ]
        return resource

    serializers = list(scan.serializer_details) + [
        SerializerIR.from_dict(value) for value in overrides.get("session_serializers", {}).values()
    ]
    for serializer in serializers:
        resources[serializer.name] = build_resource(serializer)
    views = {}
    for view in scan.view_details:
        value = asdict(view)
        value["conditional"] = capture_conditional(view.carryover)
        value["response_overrides"] = capture_response_overrides(view.carryover)
        value.pop("carryover", None)
        value["listing"] = overrides["listing"][view.name]
        auth = overrides.get("session_auth", {}).get(view.name) or value.get("auth")
        value["auth"] = auth
        if auth and auth.get("token_keyword"):
            if view.name not in overrides["auth_messages"]:
                raise ValueError("token authentication messages were not captured")
            auth["messages"] = overrides["auth_messages"][view.name]
            token_columns = {
                c["attribute"]: c["name"] for c in tables[auth["token_db_table"]]["columns"]
            }
            auth["token_user_column"] = token_columns[auth["token_user_column"]]
            source_route = next(r for r in scan.routes if r.view == view.name and r.serializer)
            assert source_route.serializer is not None
            resource = resources[source_route.serializer]
            attributes = {c["attribute"]: c["name"] for c in resource["columns"]}
            for key in ("owner_attname", "inject_owner_attname"):
                if auth[key]:
                    auth[key] = attributes[auth[key]]
        views[view.name] = value
    # Only portable runtime facts enter generated content: no source paths, DB names or scan hashes.
    routes = [
        {
            key: value
            for key, value in asdict(route).items()
            if key in {"method", "path", "view", "operation", "serializer", "options"}
        }
        for route in scan.routes
    ]
    contract = {
        "routes": routes,
        "patterns": overrides["patterns"],
        "resources": resources,
        "views": views,
        "api_roots": [asdict(r) for r in scan.api_roots],
        "generic_messages": dict(scan.generic_messages),
        "http_security": scan.http_security,
        "middleware": overrides["middleware"],
        "request_body_limit": overrides["request_body_limit"],
        "format_query_param": overrides["format_query_param"],
        "not_found_response": overrides["not_found_response"],
        "server_error_response": overrides["server_error_response"],
        "schema": {"tables": schema["tables"]},
        "collected_delete": any(
            column.get("references", {}).get("on_delete") not in {None, "DO_NOTHING"}
            for table in schema["tables"]
            for column in table["columns"]
            if column.get("references")
        ),
        "module_prefix": imports,
    }
    validator = (
        "# SPDX-License-Identifier: Apache-2.0\n"
        "from decimal import Decimal, InvalidOperation\nfrom typing import Any\n"
        "from .native_runtime import _Input, _ValidationError\n\n"
        'class ScalarInput:\n    non_field_errors = "non_field_errors"\n'
        '    messages = {"invalid": "Invalid data. Expected a dictionary, but got {datatype}."}\n'
        + inspect.getsource(ModelInput.clean_field)
    )
    files = {
        "target_app.py": "# Generated by Sanka. Configure SANKA_DATABASE_URL before serving.\n"
        f"from {imports}sanka_native.sqlalchemy_runtime import create_app\n",
        "sanka_native/__init__.py": "",
        "sanka_native/native_runtime.py": inspect.getsource(native_runtime),
        "sanka_native/sqlalchemy_middleware.py": Path(__file__)
        .with_name("sqlalchemy_middleware.py")
        .read_text(),
        "sanka_native/sqlalchemy_listing.py": Path(__file__)
        .with_name("sqlalchemy_listing.py")
        .read_text(),
        "sanka_native/scalars.py": validator,
        "sanka_native/sqlalchemy_runtime.py": Path(__file__)
        .with_name("sqlalchemy_runtime.py")
        .read_text(),
        "native_contract.json": json.dumps(contract, sort_keys=True, indent=2, allow_nan=False)
        + "\n",
    }
    if any(v.get("auth", {}).get("kind") == "session" for v in views.values()):
        files["sanka_native/sqlalchemy_sessions.py"] = (
            Path(__file__).with_name("sqlalchemy_sessions.py").read_text()
        )
    if any(
        r.get("create_contract", {}).get("style") == "nested"
        for r in resources.values()
        if r.get("create_contract")
    ):
        files["sanka_native/sqlalchemy_nested.py"] = (
            Path(__file__).with_name("sqlalchemy_nested.py").read_text()
        )
    if any(v["access"] for v in views.values()):
        files["sanka_native/sqlalchemy_access.py"] = (
            Path(__file__).with_name("sqlalchemy_access.py").read_text()
        )
    if any(v["conditional"] or v["response_overrides"] for v in views.values()):
        files["sanka_native/sqlalchemy_carryover.py"] = (
            Path(__file__).with_name("sqlalchemy_carryover.py").read_text()
        )
    if contract["collected_delete"]:
        files["sanka_native/sqlalchemy_deletion.py"] = (
            Path(__file__).with_name("sqlalchemy_deletion.py").read_text()
        )
    if module_prefix:
        return {
            **{module_prefix + "/" + name: content for name, content in files.items()},
            "target_app.py": f"from {module_prefix}.target_app import create_app\n",
        }
    return files

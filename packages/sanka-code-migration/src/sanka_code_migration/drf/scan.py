# SPDX-License-Identifier: Apache-2.0
"""Shared source capture with the legacy FastAPI scan artifact contract.

The `native` flags and adaptation reasons preserve historical FastAPI qualification,
not approval for another target. Callers must execute introspection in an isolated
source worker; this extraction does not add a sandbox to direct Python calls.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import os
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import replace as replace_dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from sanka_code_migration.drf.model import (
    ApiRootIR,
    DatabaseIR,
    FrameworkRisk,
    FrameworkScan,
    RouteAdaptationReason,
    RouteIR,
    SerializerFieldIR,
    SerializerIR,
    SkippedRoute,
    ViewAuthIR,
    ViewIR,
)

from .metadata import route_options_metadata
from .parity import route_parity_notes

DEFAULT_ARTIFACT_DIR = ".sanka"


SCAN_FILE = "scan.json"


_SUPPORTED_VIEWSET_ACTIONS = {
    "list",
    "create",
    "retrieve",
    "update",
    "partial_update",
    "destroy",
}


_KNOWN_SAFE_MIDDLEWARE = frozenset(
    {
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "django.contrib.messages.middleware.MessageMiddleware",
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.middleware.clickjacking.XFrameOptionsMiddleware",
        "django.middleware.common.CommonMiddleware",
        "django.middleware.csrf.CsrfViewMiddleware",
        "django.middleware.security.SecurityMiddleware",
    }
)


class FrameworkMigrationError(RuntimeError):
    """Raised when a framework migration cannot proceed safely."""


def _capture_http_security(settings: Any, middleware: tuple[str, ...]) -> dict[str, Any]:
    security_middleware = "django.middleware.security.SecurityMiddleware" in middleware
    frame_middleware = "django.middleware.clickjacking.XFrameOptionsMiddleware" in middleware
    common_middleware = "django.middleware.common.CommonMiddleware" in middleware
    return {
        "allowed_hosts": [str(host) for host in settings.ALLOWED_HOSTS],
        # Django itself never sets Content-Length; CommonMiddleware adds it. The generated
        # app mirrors whichever the source did so header-level parity holds.
        "content_length": common_middleware,
        # CommonMiddleware redirects slash-less requests to the slashed route (301) when
        # APPEND_SLASH holds; without it Django answers its default 404 page instead.
        "append_slash": bool(common_middleware and getattr(settings, "APPEND_SLASH", True)),
        "ssl_redirect": bool(security_middleware and settings.SECURE_SSL_REDIRECT),
        "content_type_nosniff": bool(security_middleware and settings.SECURE_CONTENT_TYPE_NOSNIFF),
        "referrer_policy": (
            str(settings.SECURE_REFERRER_POLICY)
            if security_middleware and settings.SECURE_REFERRER_POLICY
            else None
        ),
        "cross_origin_opener_policy": (
            str(settings.SECURE_CROSS_ORIGIN_OPENER_POLICY)
            if security_middleware and settings.SECURE_CROSS_ORIGIN_OPENER_POLICY
            else None
        ),
        "hsts_seconds": int(settings.SECURE_HSTS_SECONDS) if security_middleware else 0,
        "hsts_include_subdomains": bool(
            security_middleware and settings.SECURE_HSTS_INCLUDE_SUBDOMAINS
        ),
        "hsts_preload": bool(security_middleware and settings.SECURE_HSTS_PRELOAD),
        "x_frame_options": str(settings.X_FRAME_OPTIONS) if frame_middleware else None,
    }


def scan_django(
    root: str | Path = ".",
    *,
    settings_module: str | None = None,
    artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR,
) -> FrameworkScan:
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise FrameworkMigrationError(f"source root is not a directory: {root_path}")
    selected_settings = settings_module or _infer_settings_module(root_path)
    django, rest_framework = _bootstrap_django(root_path, selected_settings)
    django_conf = importlib.import_module("django.conf")
    middleware = tuple(str(item) for item in django_conf.settings.MIDDLEWARE)
    django_urls = importlib.import_module("django.urls")
    resolver = django_urls.get_resolver()
    walk = _walk_patterns(resolver.url_patterns, root_path=root_path, middleware=middleware)
    routes, risks = walk.routes, walk.risks
    if not routes:
        raise FrameworkMigrationError(
            "no Django REST Framework routes were detected; check DJANGO_SETTINGS_MODULE"
        )
    serializers = sorted({value for route in routes if (value := route.serializer)})
    models = sorted({value for route in routes if (value := route.model)})
    permissions = sorted({value for route in routes for value in route.permissions})
    authentication = sorted({value for route in routes for value in route.authentication})
    scan = FrameworkScan(
        schema_version=10,
        source=".",
        language="python",
        framework="django-rest-framework",
        python_version=".".join(str(value) for value in sys.version_info[:3]),
        django_version=str(django.get_version()),
        drf_version=str(rest_framework.VERSION),
        settings_module=selected_settings,
        root_urlconf=str(resolver.urlconf_name),
        routes=tuple(sorted(routes, key=lambda route: (route.path, route.method))),
        serializers=tuple(serializers),
        models=tuple(models),
        permissions=tuple(permissions),
        authentication=tuple(authentication),
        test_files=_count_test_files(root_path),
        risks=tuple(risks),
        serializer_details=tuple(
            walk.serializer_details[name] for name in sorted(walk.serializer_details)
        ),
        api_roots=tuple(sorted(walk.api_roots, key=lambda item: item.path)),
        view_details=tuple(walk.view_details[name] for name in sorted(walk.view_details)),
        middleware=middleware,
        http_security=_capture_http_security(django_conf.settings, middleware),
        generic_messages=_generic_messages(),
        database=_capture_database(root_path),
        skipped_routes=tuple(
            sorted(walk.skipped_routes, key=lambda item: (item.pattern, item.view))
        ),
        status_codes=_status_codes(),
    ).with_hash()
    _write_json(_artifact_path(root_path, artifact_dir, SCAN_FILE), scan.to_dict())
    return scan


def load_framework_scan(
    root: str | Path = ".", *, artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR
) -> FrameworkScan:
    path = _artifact_path(Path(root).resolve(), artifact_dir, SCAN_FILE)
    payload = _read_json(path, label="scan")
    scan = FrameworkScan.from_dict(payload)
    expected = scan.with_hash().scan_hash
    if not scan.scan_hash or scan.scan_hash != expected:
        raise FrameworkMigrationError(
            "scan artifact hash does not match its contents; run `sanka scan`"
        )
    return scan


def _field_timezone_name(field: Any) -> str | None:
    """The zone DRF would apply to this field: an explicit default or the current one."""
    explicit = getattr(field, "timezone", None)
    if explicit is not None:
        return str(getattr(explicit, "key", None) or explicit)
    django_conf = importlib.import_module("django.conf")
    if not django_conf.settings.USE_TZ:
        return None
    timezone_module = importlib.import_module("django.utils.timezone")
    return str(timezone_module.get_current_timezone_name())


def _infer_settings_module(root: Path) -> str:
    manage = root / "manage.py"
    candidates = [manage] if manage.is_file() else []
    candidates.extend(path for path in root.glob("*/wsgi.py") if path.is_file())
    candidates.extend(path for path in root.glob("*/asgi.py") if path.is_file())
    for path in candidates:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "DJANGO_SETTINGS_MODULE" not in line:
                continue
            strings = re.findall(r"['\"]([^'\"]+)['\"]", line)
            values = [value for value in strings if value != "DJANGO_SETTINGS_MODULE"]
            if values:
                return str(values[-1])
    raise FrameworkMigrationError(
        "could not infer DJANGO_SETTINGS_MODULE; pass `sanka scan --settings your_project.settings`"
    )


def _capture_database(root: Path) -> DatabaseIR:
    django_conf = importlib.import_module("django.conf")
    db = django_conf.settings.DATABASES.get("default") or {}
    engine = str(db.get("ENGINE") or "")
    if "sqlite" in engine:
        vendor = "sqlite"
    elif "postgresql" in engine or "postgis" in engine:
        vendor = "postgresql"
    elif "mysql" in engine:
        vendor = "mysql"
    else:
        vendor = "other"
    name = str(db.get("NAME") or "")
    if vendor == "sqlite" and name:
        path = Path(name)
        path = path.resolve() if path.is_absolute() else (root / path).resolve()
        try:
            name = str(path.relative_to(root.resolve()))
        except ValueError:
            name = str(path)
    return DatabaseIR(
        vendor=vendor,
        name=name,
        host=str(db.get("HOST") or ""),
        port=str(db.get("PORT") or ""),
        user=str(db.get("USER") or ""),
    )


def _bootstrap_django(root: Path, settings_module: str) -> tuple[ModuleType, ModuleType]:
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    os.environ["DJANGO_SETTINGS_MODULE"] = settings_module
    try:
        django = importlib.import_module("django")
        rest_framework = importlib.import_module("rest_framework")
    except ModuleNotFoundError as error:
        raise FrameworkMigrationError(
            "Django and djangorestframework must be installed in the project environment"
        ) from error
    django.setup()
    return django, rest_framework


class _WalkResult:
    def __init__(self) -> None:
        self.routes: list[RouteIR] = []
        self.risks: list[FrameworkRisk] = []
        self.serializer_details: dict[str, SerializerIR] = {}
        self.api_roots: list[ApiRootIR] = []
        self.view_details: dict[str, ViewIR] = {}
        self.skipped_routes: list[SkippedRoute] = []


def _walk_patterns(
    patterns: Iterable[Any],
    *,
    root_path: Path,
    middleware: tuple[str, ...],
    prefix: str = "",
    collector: _WalkResult | None = None,
) -> _WalkResult:
    result = collector if collector is not None else _WalkResult()
    for pattern in patterns:
        raw = str(pattern.pattern)
        combined = f"{prefix}{raw}"
        nested = getattr(pattern, "url_patterns", None)
        if nested is not None:
            _walk_patterns(
                nested,
                root_path=root_path,
                middleware=middleware,
                prefix=combined,
                collector=result,
            )
            continue
        callback = getattr(pattern, "callback", None)
        view_class = getattr(callback, "cls", None) or getattr(callback, "view_class", None)
        if callback is None or view_class is None or not _is_drf_view(view_class):
            # A non-DRF callback is outside the scan's vocabulary, but silence
            # here hides a real route from every downstream disclosure. Record
            # it so plans, manifests, and gap reports can say "this exists and
            # was never scanned" instead of pretending the URL space ends at
            # DRF's edge.
            if callback is not None:
                view_name = _qualified_name(view_class or callback) or repr(callback)
                result.skipped_routes.append(
                    SkippedRoute(pattern=combined, view=view_name, reason="non-drf-view")
                )
            continue
        path, supported = _to_fastapi_path(combined)
        _normalized, _groups_supported, named_regexes = _replace_named_regex_groups(combined)
        source_file, source_line = _source_location(view_class, root_path)
        view_name = f"{view_class.__module__}.{view_class.__qualname__}"
        serializer = _qualified_name(getattr(view_class, "serializer_class", None))
        queryset = getattr(view_class, "queryset", None)
        model = _qualified_name(getattr(queryset, "model", None))
        permissions = tuple(
            value
            for item in getattr(view_class, "permission_classes", ())
            if (value := _qualified_name(item))
        )
        authentication = tuple(
            value
            for item in getattr(view_class, "authentication_classes", ())
            if (value := _qualified_name(item))
        )
        actions = getattr(callback, "actions", None)
        if actions is None:
            actions = _generic_actions(view_class)
        methods = _route_methods(view_class, actions)
        if not supported:
            result.risks.append(
                FrameworkRisk(
                    severity="high",
                    code="SANKA_DRF_DYNAMIC_ROUTE",
                    message=f"Route pattern requires manual adaptation: {combined}",
                    file=source_file,
                    line=source_line,
                )
            )
        native, adaptation_reasons, manual_operations = _native_route_support(
            result,
            view_class=view_class,
            callback=callback,
            actions=actions,
            path=path,
            supported=supported,
            serializer_name=serializer,
            middleware=middleware,
        )
        captured_view = result.view_details.get(view_name)
        if captured_view is not None and captured_view.access:
            converters = importlib.import_module("django.urls.converters").get_converters()
            parameter_regexes = {
                name: converters[kind].regex
                for kind, name in re.findall(
                    r"<(str|int|slug|uuid|path):([A-Za-z_][A-Za-z0-9_]*)>", combined
                )
            }
            result.view_details[view_name] = replace_dataclass(
                captured_view,
                access={**captured_view.access, "parameter_regexes": parameter_regexes},
            )
        if captured_view is not None and captured_view.access.get("member_action"):
            serializer = captured_view.access["serializer"]
        lookup_kwarg = str(
            getattr(view_class, "lookup_url_kwarg", None)
            or getattr(view_class, "lookup_field", "pk")
        )
        lookup_regex = named_regexes.get(lookup_kwarg)
        if lookup_regex is not None and view_name in result.view_details:
            detail = result.view_details[view_name]
            if detail.lookup_regex not in {None, lookup_regex}:
                native = False
                adaptation_reasons = (
                    *adaptation_reasons,
                    _adaptation_reason(
                        "SANKA_DRF_LOOKUP_REGEX_CONFLICT",
                        "route-pattern",
                        f"Lookup URL kwarg {lookup_kwarg!r} uses multiple regexes.",
                    ),
                )
            else:
                result.view_details[view_name] = replace_dataclass(
                    detail, lookup_regex=lookup_regex
                )
        options_metadata = route_options_metadata(
            view_class=view_class, callback=callback, actions=actions, path=path
        )
        for method, operation in methods:
            operation_source = _safe_source(getattr(view_class, operation, None))
            transactional = (
                "transaction.atomic" in operation_source or "@atomic" in operation_source
            )
            parity_notes = route_parity_notes(
                view_class=view_class,
                callback=callback,
                actions=actions,
                method=method,
                operation=operation,
                path=path,
                middleware=middleware,
                root_path=root_path,
            )
            route_native = native and operation not in manual_operations
            route_reasons = (*adaptation_reasons, *manual_operations.get(operation, ()))
            result.routes.append(
                RouteIR(
                    method=method,
                    path=path,
                    operation=operation,
                    view=view_name,
                    serializer=serializer,
                    model=model,
                    authentication=authentication,
                    permissions=permissions,
                    transactional=transactional,
                    source_file=source_file,
                    source_line=source_line,
                    supported=supported,
                    native=route_native,
                    adaptation_reasons=route_reasons,
                    parity_notes=parity_notes,
                    options=options_metadata,
                )
            )
    result.routes = list({route.key: route for route in result.routes}.values())
    result.skipped_routes = list(
        {(item.pattern, item.view): item for item in result.skipped_routes}.values()
    )
    return result


def _is_drf_view(view_class: type[Any]) -> bool:
    return any(base.__module__.startswith("rest_framework.") for base in inspect.getmro(view_class))


def _is_format_alias_path(path: str) -> bool:
    return "{format}" in path or "drf_format_suffix" in path


def _adaptation_reason(code: str, feature: str, message: str) -> RouteAdaptationReason:
    return RouteAdaptationReason(code=code, feature=feature, message=message)


def _middleware_adaptation_reason(
    middleware: tuple[str, ...],
) -> RouteAdaptationReason:
    count = len(middleware)
    noun = "class is" if count == 1 else "classes are"
    return _adaptation_reason(
        "SANKA_DRF_MIDDLEWARE_UNSUPPORTED",
        "middleware",
        f"{count} Django middleware {noun} outside the native allowlist: " + ", ".join(middleware),
    )


def _unsupported_middleware(middleware: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(item for item in middleware if item not in _KNOWN_SAFE_MIDDLEWARE)


def _qualified_items(values: Iterable[Any]) -> str:
    names = [name for item in values if (name := _qualified_name(item))]
    return ", ".join(names) if names else "none"


def _not_native(
    code: str, feature: str, message: str
) -> tuple[bool, tuple[RouteAdaptationReason, ...], dict[str, tuple[RouteAdaptationReason, ...]]]:
    return False, (_adaptation_reason(code, feature, message),), {}


def _native_route_support(
    result: _WalkResult,
    *,
    view_class: type[Any],
    callback: Any,
    actions: dict[str, str] | None,
    path: str,
    supported: bool,
    serializer_name: str | None,
    middleware: tuple[str, ...],
) -> tuple[bool, tuple[RouteAdaptationReason, ...], dict[str, tuple[RouteAdaptationReason, ...]]]:
    """Decide whether a route sits inside the native-generation envelope.

    The envelope is deliberately narrow and checked against the live classes,
    not names: default-behavior ModelViewSet CRUD over a captured
    ModelSerializer, or the router API root. Anything else must be adapted by
    a human, never silently bridged in a native plan.
    """
    if _is_format_alias_path(path):
        return False, (), {}
    if not supported:
        return _not_native(
            "SANKA_DRF_ROUTE_PATTERN_UNSUPPORTED",
            "route-pattern",
            "The route pattern cannot be represented safely as a FastAPI path.",
        )
    unsupported_middleware = _unsupported_middleware(middleware)
    middleware_reasons = (
        (_middleware_adaptation_reason(unsupported_middleware),) if unsupported_middleware else ()
    )

    # Per-operation override reasons ride along with every verdict, so a route whose
    # action is overridden explains both the view-level and the action-level gap.
    manual_operations: dict[str, tuple[RouteAdaptationReason, ...]] = {}

    def disqualify(
        code: str, feature: str, message: str
    ) -> tuple[
        bool, tuple[RouteAdaptationReason, ...], dict[str, tuple[RouteAdaptationReason, ...]]
    ]:
        return (
            False,
            (*middleware_reasons, _adaptation_reason(code, feature, message)),
            manual_operations,
        )

    routers = importlib.import_module("rest_framework.routers")
    if inspect.isclass(view_class) and issubclass(view_class, routers.APIRootView):
        permissions_module = importlib.import_module("rest_framework.permissions")
        root_permissions = getattr(view_class, "permission_classes", ())
        if any(item is not permissions_module.AllowAny for item in root_permissions):
            return disqualify(
                "SANKA_DRF_API_ROOT_PERMISSIONS_UNSUPPORTED",
                "permissions",
                "Router API root permissions are outside AllowAny: "
                + _qualified_items(root_permissions),
            )
        links = _api_root_links(callback)
        if links is None:
            return disqualify(
                "SANKA_DRF_API_ROOT_LINKS_UNRESOLVED",
                "router-registration",
                "Router API root links could not be resolved from the registered routes.",
            )
        if middleware_reasons:
            return False, middleware_reasons, {}
        if all(root.path != path for root in result.api_roots):
            result.api_roots.append(ApiRootIR(path=path, links=links))
        return True, (), {}
    from sanka_code_migration.drf.access_contracts import (
        capture_member_action,
        capture_membership_permission,
    )

    member_action = capture_member_action(view_class)
    if member_action is not None:
        model_class, member_serializer, contract = member_action
        view_name = f"{view_class.__module__}.{view_class.__qualname__}"
        auth_ir = _view_auth_support(view_class, model_class)
        permission_rules = [
            capture_membership_permission(p, model_class) for p in view_class.permission_classes
        ]
        rule = next((p for p in permission_rules if p is not None), None)
        if (
            auth_ir is None
            or rule is None
            or rule.get("parent_param")
            or getattr(callback, "view_initkwargs", None)
            or getattr(callback, "initkwargs", None)
            or getattr(view_class, "throttle_classes", ())
            or getattr(view_class, "versioning_class", None)
            or middleware_reasons
            or f"{{{contract['param']}}}" not in path
        ):
            return disqualify(
                "SANKA_DRF_MEMBER_ACTION_UNSUPPORTED",
                "member-action",
                "Member action requires supported authentication and object permissions.",
            )
        serializer_name = f"{member_serializer.__module__}.{member_serializer.__qualname__}"
        field = importlib.import_module("rest_framework.fields").UUIDField(read_only=True)
        if model_class._meta.pk.get_internal_type() != "UUIDField":
            field = importlib.import_module("rest_framework.fields").IntegerField(read_only=True)
        member_ir = SerializerIR(
            name=serializer_name,
            model=f"{model_class.__module__}.{model_class.__qualname__}",
            model_module=model_class.__module__,
            model_class=model_class.__qualname__,
            object_name=model_class._meta.object_name,
            db_table=model_class._meta.db_table,
            pk_attname=model_class._meta.pk.attname,
            fields=(_serializer_field_ir(model_class._meta.pk.name, field, model_class),),
        )
        result.serializer_details[serializer_name] = member_ir
        result.view_details[view_name] = ViewIR(
            name=view_name,
            auth=auth_ir,
            lookup_url_kwarg=contract["param"],
            access={"permission": rule, "member_action": contract, "serializer": serializer_name},
        )
        return True, (), {}
    generics = importlib.import_module("rest_framework.generics")
    generic_view = issubclass(view_class, generics.GenericAPIView) and not hasattr(
        callback, "actions"
    )
    if generic_view:
        generic_reason = _generic_view_adaptation_reason(view_class, callback)
        if generic_reason is not None:
            return False, (*middleware_reasons, generic_reason), {}
    if actions is None:
        return disqualify(
            "SANKA_DRF_VIEW_KIND_UNSUPPORTED",
            "view-kind",
            f"{view_class.__module__}.{view_class.__qualname__} is not router-bound "
            "ModelViewSet CRUD.",
        )
    unsupported_actions = sorted(set(actions.values()) - _SUPPORTED_VIEWSET_ACTIONS)
    if unsupported_actions:
        return disqualify(
            "SANKA_DRF_CUSTOM_ACTION_UNSUPPORTED",
            "viewset-actions",
            "Custom or unsupported viewset actions are present: " + ", ".join(unsupported_actions),
        )
    viewsets = importlib.import_module("rest_framework.viewsets")
    if not generic_view and not issubclass(view_class, viewsets.ModelViewSet):
        return disqualify(
            "SANKA_DRF_VIEWSET_KIND_UNSUPPORTED",
            "view-kind",
            f"{view_class.__module__}.{view_class.__qualname__} is not a ModelViewSet.",
        )
    from sanka_code_migration.drf.access_contracts import capture_query

    source_model = getattr(
        getattr(getattr(view_class, "serializer_class", None), "Meta", None), "model", None
    )
    access: dict[str, Any] = {}
    overrides = _viewset_overrides(view_class, allow_missing=generic_view)
    if "get_queryset" in overrides:
        query = capture_query(view_class, source_model)
        if query is not None:
            access["query"] = query
            overrides = tuple(name for name in overrides if name != "get_queryset")
    operation_overrides = [name for name in overrides if name in _OPERATION_OVERRIDES]
    if "update" in operation_overrides and "partial_update" not in operation_overrides:
        operation_overrides.append("partial_update")
    structural_overrides = [name for name in overrides if name not in _OPERATION_OVERRIDES]
    if structural_overrides:
        return disqualify(
            "SANKA_DRF_VIEWSET_OVERRIDES_UNSUPPORTED",
            "viewset-overrides",
            "Viewset overrides require manual carryover: " + ", ".join(structural_overrides),
        )
    # An overridden action keeps only that route manual; the rest of the viewset stays
    # native, which is what makes partially customised viewsets worth generating.
    manual_operations = {
        name: (
            _adaptation_reason(
                "SANKA_DRF_VIEWSET_OVERRIDES_UNSUPPORTED",
                "viewset-overrides",
                f"Viewset override requires manual carryover: {name}",
            ),
        )
        for name in operation_overrides
    }
    view_name = f"{view_class.__module__}.{view_class.__qualname__}"
    if view_name not in result.view_details:
        queryset = getattr(view_class, "queryset", None)
        model = getattr(queryset, "model", None) or source_model
        auth_ir = _view_auth_support(view_class, model)
        from sanka_code_migration.drf.access_contracts import (
            capture_creator_membership,
            capture_membership_permission,
        )

        for permission in view_class.permission_classes:
            captured = capture_membership_permission(permission, model)
            if captured is not None:
                access["permission"] = captured
        creator = capture_creator_membership(view_class, model)
        if creator is not None:
            access["creator_membership"] = creator
        result.view_details[view_name] = ViewIR(name=view_name, auth=auth_ir, access=access)
    detail = result.view_details[view_name]
    user_scoped = any(
        rule["kind"] == "member" for rule in detail.access.get("query", {}).get("filters", ())
    )
    if user_scoped and (detail.auth is None or not detail.auth.require_authenticated):
        return disqualify(
            "SANKA_DRF_USER_QUERY_AUTH_UNSUPPORTED",
            "authentication",
            "User-scoped querysets require an explicit authenticated-user gate.",
        )
    if result.view_details[view_name].auth is None:
        return disqualify(
            "SANKA_DRF_AUTH_PERMISSIONS_UNSUPPORTED",
            "authentication-permissions",
            "Authentication/permission classes are outside AllowAny or the supported "
            "TokenAuthentication + IsAuthenticated owner pattern; authentication: "
            + _qualified_items(getattr(view_class, "authentication_classes", ()))
            + "; permissions: "
            + _qualified_items(getattr(view_class, "permission_classes", ())),
        )
    throttle_classes = tuple(getattr(view_class, "throttle_classes", ()))
    if throttle_classes:
        return disqualify(
            "SANKA_DRF_THROTTLING_UNSUPPORTED",
            "throttling",
            "Throttle classes require manual adaptation: " + _qualified_items(throttle_classes),
        )
    if getattr(view_class, "versioning_class", None) is not None:
        return disqualify(
            "SANKA_DRF_VERSIONING_UNSUPPORTED",
            "versioning",
            "Versioning class requires manual adaptation: "
            + _qualified_items((view_class.versioning_class,)),
        )
    if serializer_name is None:
        return disqualify(
            "SANKA_DRF_SERIALIZER_MISSING",
            "serializer",
            "No serializer_class was captured for this ModelViewSet.",
        )
    ir = result.serializer_details.get(serializer_name)
    if ir is None:
        ir = _serializer_ir(view_class, serializer_name)
        if ir is None:
            return disqualify(
                "SANKA_DRF_SERIALIZER_UNSUPPORTED",
                "serializer",
                f"Serializer cannot be captured as ModelSerializer CRUD: {serializer_name}",
            )
        result.serializer_details[serializer_name] = ir
    if not ir.supported:
        return disqualify(
            "SANKA_DRF_SERIALIZER_SEMANTICS_UNSUPPORTED",
            "serializer",
            f"Serializer fields, validation, queryset, or write overrides require manual "
            f"adaptation: {serializer_name}",
        )
    listing, listing_reason = _listing_support(view_class, ir)
    if listing_reason is not None:
        return False, (*middleware_reasons, listing_reason), {}
    carryover: dict[str, Any] = {}
    if manual_operations:
        carried = _analyze_view_carryover(view_class, list(manual_operations))
        if carried is not None:
            carryover = carried
            manual_operations = {}
    result.view_details[view_name] = replace_dataclass(
        result.view_details[view_name],
        listing=listing,
        carryover=carryover,
        lookup_url_kwarg=getattr(view_class, "lookup_url_kwarg", None),
    )
    if result.view_details[view_name].access:
        from sanka_code_migration.drf.access_contracts import capture_delete
        from sanka_code_migration.drf.nested_create import supports_default_model_writes

        if not supports_default_model_writes(source_model):
            for operation in {"create", "update", "partial_update", "destroy"}.intersection(
                actions.values()
            ):
                manual_operations[operation] = (
                    _adaptation_reason(
                        "SANKA_DRF_MODEL_WRITES_UNSUPPORTED",
                        "model-writes",
                        "Custom model write behavior requires manual adaptation.",
                    ),
                )
        deletion = capture_delete(source_model)
        detail = result.view_details[view_name]
        if deletion is not None:
            result.view_details[view_name] = replace_dataclass(
                detail, access={**detail.access, "delete_tables": deletion}
            )
        elif "destroy" in actions.values():
            manual_operations["destroy"] = (
                _adaptation_reason(
                    "SANKA_DRF_DELETE_CASCADE_UNSUPPORTED",
                    "delete-cascade",
                    "Delete hooks or cascading relations require manual adaptation.",
                ),
            )
    lookup_reason = _custom_lookup_adaptation_reason(
        view_class,
        actions=actions,
        path=path,
        serializer=ir,
    )
    if lookup_reason is not None:
        return False, (*middleware_reasons, lookup_reason), {}
    if middleware_reasons:
        return False, middleware_reasons, {}
    return True, (), manual_operations


def _custom_lookup_adaptation_reason(
    view_class: type[Any],
    *,
    actions: dict[str, str],
    path: str,
    serializer: SerializerIR,
) -> RouteAdaptationReason | None:
    """Validate the narrow custom-lookup envelope used by native CRUD.

    A primary-key lookup may use a separate URL kwarg. Other detail lookups
    must name the same URL kwarg and unique model field, with a scalar serializer
    field whose value the generated stores can coerce without Django.
    This is intentionally narrower than everything
    DRF accepts: it prevents a generated ``get()`` from changing one-object
    semantics or quietly querying the primary key instead.
    """
    lookup_field = str(getattr(view_class, "lookup_field", "pk") or "pk")
    lookup_kwarg = str(getattr(view_class, "lookup_url_kwarg", None) or lookup_field)
    if lookup_field == "pk" and lookup_kwarg == "pk":
        return None
    if not lookup_field.isidentifier() or not lookup_kwarg.isidentifier():
        return _adaptation_reason(
            "SANKA_DRF_LOOKUP_NAME_UNSUPPORTED",
            "lookup-field",
            f"Lookup names must be Python identifiers: field={lookup_field!r}, "
            f"URL kwarg={lookup_kwarg!r}.",
        )
    if lookup_field != "pk" and lookup_kwarg != lookup_field:
        return _adaptation_reason(
            "SANKA_DRF_LOOKUP_URL_KWARG_UNSUPPORTED",
            "lookup-field",
            "Native generation currently requires lookup_url_kwarg to match "
            f"lookup_field; got {lookup_kwarg!r} and {lookup_field!r}.",
        )
    detail_actions = {"retrieve", "update", "partial_update", "destroy"}
    if detail_actions.intersection(actions.values()) and f"{{{lookup_kwarg}}}" not in path:
        return _adaptation_reason(
            "SANKA_DRF_LOOKUP_PATH_UNRESOLVED",
            "lookup-field",
            f"Detail route {path!r} does not expose the lookup kwarg {lookup_kwarg!r}.",
        )
    if lookup_field == "pk":
        return None
    queryset = getattr(view_class, "queryset", None)
    model = getattr(queryset, "model", None)
    if model is None:
        return _adaptation_reason(
            "SANKA_DRF_LOOKUP_FIELD_MISSING",
            "lookup-field",
            f"Model field {lookup_field!r} could not be resolved.",
        )
    django_exceptions = importlib.import_module("django.core.exceptions")
    try:
        model_field = model._meta.get_field(lookup_field)
    except django_exceptions.FieldDoesNotExist:
        return _adaptation_reason(
            "SANKA_DRF_LOOKUP_FIELD_MISSING",
            "lookup-field",
            f"Model field {lookup_field!r} could not be resolved.",
        )
    if not (getattr(model_field, "unique", False) or getattr(model_field, "primary_key", False)):
        return _adaptation_reason(
            "SANKA_DRF_LOOKUP_FIELD_NOT_UNIQUE",
            "lookup-field",
            f"Custom lookup field {lookup_field!r} is not unique.",
        )
    serializer_field = next(
        (field for field in serializer.fields if field.name == lookup_field),
        None,
    )
    if serializer_field is None:
        return _adaptation_reason(
            "SANKA_DRF_LOOKUP_FIELD_NOT_SERIALIZED",
            "lookup-field",
            f"Custom lookup field {lookup_field!r} is absent from the serializer.",
        )
    if not serializer_field.supported or serializer_field.kind not in {
        "char",
        "integer",
        "big_integer",
    }:
        return _adaptation_reason(
            "SANKA_DRF_LOOKUP_TYPE_UNSUPPORTED",
            "lookup-field",
            f"Custom lookup field {lookup_field!r} has unsupported native kind "
            f"{serializer_field.kind!r}.",
        )
    return None


def _generic_actions(view_class: type[Any]) -> dict[str, str] | None:
    """Map only stock concrete GenericAPIView HTTP handlers to CRUD operations.

    Identity checks deliberately reject custom get/post wrappers, even when a
    method name resembles a standard DRF operation.
    """
    generics = importlib.import_module("rest_framework.generics")
    concrete = {
        "CreateAPIView": {"post": "create"},
        "ListAPIView": {"get": "list"},
        "RetrieveAPIView": {"get": "retrieve"},
        "DestroyAPIView": {"delete": "destroy"},
        "UpdateAPIView": {"put": "update", "patch": "partial_update"},
        "ListCreateAPIView": {"get": "list", "post": "create"},
        "RetrieveUpdateAPIView": {"get": "retrieve", "put": "update", "patch": "partial_update"},
        "RetrieveDestroyAPIView": {"get": "retrieve", "delete": "destroy"},
        "RetrieveUpdateDestroyAPIView": {
            "get": "retrieve",
            "put": "update",
            "patch": "partial_update",
            "delete": "destroy",
        },
    }
    if not issubclass(view_class, generics.GenericAPIView):
        return None
    handlers = {
        (method, getattr(getattr(generics, name), method)): action
        for name, methods in concrete.items()
        for method, action in methods.items()
    }
    actions: dict[str, str] = {}
    for method in getattr(view_class, "http_method_names", ()):
        handler = getattr(view_class, method, None)
        if method in {"head", "options", "trace"} or handler is None:
            continue
        action = handlers.get((method, handler))
        if action is None:
            return None
        actions[method] = action
    return actions or None


def _generic_view_adaptation_reason(
    view_class: type[Any], callback: Any
) -> RouteAdaptationReason | None:
    views = importlib.import_module("rest_framework.views")
    # Per-URL constructor overrides must not be lost when reading class-level
    # queryset, serializer, permissions or pagination configuration.
    if getattr(callback, "view_initkwargs", None) or getattr(callback, "initkwargs", None):
        return _adaptation_reason(
            "SANKA_DRF_GENERIC_INITKWARGS_UNSUPPORTED",
            "view-configuration",
            "Generic view as_view() overrides require manual adaptation.",
        )
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
        "setup",
        "options",
    )
    overrides = [
        name for name in hooks if getattr(view_class, name) is not getattr(views.APIView, name)
    ]
    generics = importlib.import_module("rest_framework.generics")
    overrides.extend(
        name
        for name in ("paginate_queryset", "get_paginated_response")
        if getattr(view_class, name) is not getattr(generics.GenericAPIView, name)
    )
    overrides.extend(
        name for name in ("head", "trace") if callable(getattr(view_class, name, None))
    )
    if overrides or _generic_actions(view_class) is None:
        return _adaptation_reason(
            "SANKA_DRF_GENERIC_HANDLERS_UNSUPPORTED",
            "view-handlers",
            "Generic view custom request handlers require manual adaptation"
            + (": " + ", ".join(overrides) if overrides else "."),
        )
    return None


def _viewset_overrides(view_class: type[Any], *, allow_missing: bool = False) -> tuple[str, ...]:
    generics = importlib.import_module("rest_framework.generics")
    mixins = importlib.import_module("rest_framework.mixins")
    expected = {
        "list": mixins.ListModelMixin.list,
        "create": mixins.CreateModelMixin.create,
        "retrieve": mixins.RetrieveModelMixin.retrieve,
        "update": mixins.UpdateModelMixin.update,
        "partial_update": mixins.UpdateModelMixin.partial_update,
        "perform_update": mixins.UpdateModelMixin.perform_update,
        "destroy": mixins.DestroyModelMixin.destroy,
        "perform_destroy": mixins.DestroyModelMixin.perform_destroy,
        "get_queryset": generics.GenericAPIView.get_queryset,
        "get_object": generics.GenericAPIView.get_object,
        "get_serializer": generics.GenericAPIView.get_serializer,
        "get_serializer_class": generics.GenericAPIView.get_serializer_class,
        "filter_queryset": generics.GenericAPIView.filter_queryset,
    }
    return tuple(
        name
        for name, func in expected.items()
        if getattr(view_class, name, None) is not func
        and not (allow_missing and not hasattr(view_class, name))
    )


_OPERATION_OVERRIDES = ("list", "create", "retrieve", "update", "partial_update", "destroy")


_SEARCH_LOOKUPS = {"^": "istartswith", "=": "iexact"}


_UNSUPPORTED_SEARCH_PREFIXES = ("@", "$")


def _defines_methods(cls: type[Any], base: type[Any]) -> bool:
    """True when ``cls`` (below ``base`` in its MRO) overrides any method."""
    for klass in inspect.getmro(cls):
        if klass is base or klass is object:
            break
        if any(inspect.isfunction(member) for member in vars(klass).values()):
            return True
    return False


def _listing_support(
    view_class: type[Any], ir: SerializerIR
) -> tuple[dict[str, Any], RouteAdaptationReason | None]:
    """Capture list semantics the native runtime reproduces, or the reason it cannot.

    Cursor pagination, SearchFilter, and OrderingFilter are DRF-generic: their exact
    behaviour follows from class attributes, so the runtime ports them. A custom
    OrderingFilter is accepted only when probing its ``get_ordering`` against the stock
    filter identifies one of the known tie-break idioms.
    """
    pagination_module = importlib.import_module("rest_framework.pagination")
    filters_module = importlib.import_module("rest_framework.filters")
    field_names = {field.name for field in ir.fields}
    text_fields = {field.name for field in ir.fields if field.kind in {"char", "choice"}}
    orderable = field_names | {"pk", ir.pk_attname}
    listing: dict[str, Any] = {}
    paginator_class: Any = getattr(view_class, "pagination_class", None)
    if paginator_class is not None:
        cursor_base = pagination_module.CursorPagination
        if not (
            inspect.isclass(paginator_class) and issubclass(paginator_class, cursor_base)
        ) or _defines_methods(paginator_class, cursor_base):
            return {}, _adaptation_reason(
                "SANKA_DRF_PAGINATION_UNSUPPORTED",
                "pagination",
                "Pagination class requires manual adaptation: "
                + _qualified_items((paginator_class,)),
            )
        paginator: Any = paginator_class()
        ordering = paginator.ordering
        terms = (ordering,) if isinstance(ordering, str) else tuple(ordering or ())
        if not terms or any(str(term).lstrip("-") not in orderable for term in terms):
            return {}, _adaptation_reason(
                "SANKA_DRF_PAGINATION_UNSUPPORTED",
                "pagination",
                f"Cursor ordering must name serializer fields: {[str(t) for t in terms]!r}",
            )
        listing["pagination"] = {
            "kind": "cursor",
            "page_size": paginator.page_size,
            "ordering": [str(term) for term in terms],
            "cursor_param": str(paginator.cursor_query_param),
            "page_size_param": paginator.page_size_query_param,
            "max_page_size": paginator.max_page_size,
            "offset_cutoff": int(paginator.offset_cutoff),
            "invalid_cursor_message": str(paginator.invalid_cursor_message),
        }
    backends: list[Any] = list(getattr(view_class, "filter_backends", ()))
    for backend in backends:
        if inspect.isclass(backend) and issubclass(backend, filters_module.SearchFilter):
            if _defines_methods(backend, filters_module.SearchFilter):
                return {}, _adaptation_reason(
                    "SANKA_DRF_FILTER_BACKENDS_UNSUPPORTED",
                    "filter-backends",
                    "Search filter subclass requires manual adaptation: "
                    + _qualified_items((backend,)),
                )
            specs: list[dict[str, str]] = []
            for raw in getattr(view_class, "search_fields", None) or ():
                text = str(raw)
                lookup = _SEARCH_LOOKUPS.get(text[:1], "icontains")
                name = text[1:] if text[:1] in _SEARCH_LOOKUPS else text
                if (
                    text[:1] in _UNSUPPORTED_SEARCH_PREFIXES
                    or "__" in name
                    or name not in text_fields
                ):
                    return {}, _adaptation_reason(
                        "SANKA_DRF_SEARCH_FIELDS_UNSUPPORTED",
                        "filter-backends",
                        f"Search field requires manual adaptation: {text}",
                    )
                specs.append({"name": name, "lookup": lookup})
            search_backend: Any = backend
            listing["search"] = {"param": str(search_backend.search_param), "fields": specs}
        elif inspect.isclass(backend) and issubclass(backend, filters_module.OrderingFilter):
            declared = getattr(view_class, "ordering_fields", None)
            if declared is None:
                declared = getattr(backend, "ordering_fields", None)
            if declared == "__all__":
                return {}, _adaptation_reason(
                    "SANKA_DRF_ORDERING_FILTER_UNSUPPORTED",
                    "filter-backends",
                    "ordering_fields = '__all__' requires manual adaptation.",
                )
            names = (
                [str(item) if isinstance(item, str) else str(item[0]) for item in declared]
                if declared
                else [field.name for field in ir.fields]
            )
            default = getattr(view_class, "ordering", None)
            default_terms = (
                (str(default),)
                if isinstance(default, str)
                else tuple(str(item) for item in default or ())
            )
            if any(name not in orderable for name in names) or any(
                term.lstrip("-") not in orderable for term in default_terms
            ):
                return {}, _adaptation_reason(
                    "SANKA_DRF_ORDERING_FILTER_UNSUPPORTED",
                    "filter-backends",
                    "Ordering fields must name serializer fields: "
                    f"{names!r} (default {list(default_terms)!r})",
                )
            pk = str(ir.pk_attname or "id")
            rule: str | None = "drf"
            if _defines_methods(backend, filters_module.OrderingFilter):
                rule = _probe_ordering_rule(backend, view_class, names, pk)
            if rule is None:
                return {}, _adaptation_reason(
                    "SANKA_DRF_ORDERING_FILTER_UNSUPPORTED",
                    "filter-backends",
                    "Ordering filter subclass has no recognised tie-break idiom: "
                    + _qualified_items((backend,)),
                )
            ordering_backend: Any = backend
            listing["ordering"] = {
                "param": str(ordering_backend.ordering_param),
                "fields": names,
                "default": list(default_terms) or None,
                "rule": rule,
                "pk": pk,
            }
        else:
            return {}, _adaptation_reason(
                "SANKA_DRF_FILTER_BACKENDS_UNSUPPORTED",
                "filter-backends",
                "Filter backends require manual adaptation: " + _qualified_items((backend,)),
            )
    return listing, None


def _probe_ordering_rule(
    backend: type[Any], view_class: type[Any], names: list[str], pk: str
) -> str | None:
    """Identify a custom OrderingFilter's behaviour by probing it against the stock one."""
    filters_module = importlib.import_module("rest_framework.filters")
    test_module = importlib.import_module("rest_framework.test")
    request_module = importlib.import_module("rest_framework.request")
    factory = test_module.APIRequestFactory()
    queryset = getattr(view_class, "queryset", None)
    param = str(backend.ordering_param)
    probes: list[dict[str, str]] = [{}, {param: "nope"}]
    probes += [{param: name} for name in names] + [{param: "-" + name} for name in names]
    probes += [{param: f"{a},-{b}"} for a in names for b in names if a != b][:6]

    def run(instance: Any, params: dict[str, str]) -> list[str] | None:
        request = request_module.Request(factory.get("/", params))
        result = instance.get_ordering(request, queryset, view_class())
        return [str(term) for term in result] if result else None

    def follow_last(base: list[str] | None) -> list[str] | None:
        if not base:
            return base
        if {term.lstrip("-") for term in base} & {pk, "pk"}:
            return list(base)
        return [*base, "-" + pk if base[-1].startswith("-") else pk]

    def ascending(base: list[str] | None) -> list[str] | None:
        if not base:
            return base
        if {term.lstrip("-") for term in base} & {pk, "pk"}:
            return list(base)
        return [*base, pk]

    reference = filters_module.OrderingFilter()
    try:
        observed = [run(backend(), probe) for probe in probes]
        baseline = [run(reference, probe) for probe in probes]
    except Exception:  # an unprobeable filter is simply unsupported
        return None
    hypotheses: tuple[tuple[str, Callable[[list[str] | None], list[str] | None]], ...] = (
        ("drf", lambda base: base),
        ("append-pk-follow-last", follow_last),
        ("append-pk-asc", ascending),
    )
    for name, hypothesis in hypotheses:
        if all(seen == hypothesis(base) for seen, base in zip(observed, baseline, strict=True)):
            return name
    return None


_VIEW_CARRYOVER_MODULES = {
    "base64",
    "collections",
    "datetime",
    "decimal",
    "functools",
    "hashlib",
    "itertools",
    "json",
    "math",
    "operator",
    "re",
    "statistics",
    "string",
    "typing",
    "uuid",
}


def _analyze_view_carryover(view_class: type[Any], operations: list[str]) -> dict[str, Any] | None:
    """Admit overridden viewset actions into the envelope by carrying them over verbatim.

    The overridden action methods, and the helpers they call on ``self``, are re-emitted
    unchanged as a subclass of the runtime's ``CarryoverView``: ``super().<action>()``
    reaches the generated handler and DRF's ``Response``/``status``/``Request`` names
    resolve to shims. Only code that touches nothing else — no ORM, no ``self.request``,
    no ``get_object``, no imports beyond the standard-library allowlist — qualifies;
    anything else keeps the action manual with its reason.
    """
    import builtins
    import textwrap
    import types

    response_module = importlib.import_module("rest_framework.response")
    status_module = importlib.import_module("rest_framework.status")
    request_module = importlib.import_module("rest_framework.request")
    own = {
        name: member
        for name, member in vars(view_class).items()
        if isinstance(member, types.FunctionType | staticmethod | classmethod)
    }
    pending = [name for name in operations if name in own]
    if not pending:
        return None
    module_globals = vars(importlib.import_module(view_class.__module__))
    carried: dict[str, str] = {}
    order: list[str] = []
    imports: dict[str, tuple[str, str | None]] = {}
    allowed_super = set(_OPERATION_OVERRIDES)
    queue = list(pending)
    while queue:
        name = queue.pop(0)
        if name in carried:
            continue
        member = own.get(name)
        if member is None:
            return None
        function = member.__func__ if isinstance(member, staticmethod | classmethod) else member
        try:
            source = textwrap.dedent(inspect.getsource(function))
        except (OSError, TypeError):
            return None
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return None
        if not tree.body or not isinstance(tree.body[0], ast.FunctionDef):
            return None
        node = tree.body[0]
        for decorator in node.decorator_list:
            if not (
                isinstance(decorator, ast.Name) and decorator.id in {"staticmethod", "classmethod"}
            ):
                return None
        positional = [item.arg for item in node.args.posonlyargs + node.args.args]
        self_name = (
            None if isinstance(member, staticmethod) else (positional[0] if positional else None)
        )
        bound: set[str] = set(positional) | {item.arg for item in node.args.kwonlyargs}
        if node.args.vararg:
            bound.add(node.args.vararg.arg)
        if node.args.kwarg:
            bound.add(node.args.kwarg.arg)
        loaded: set[str] = set()
        super_attributes: set[int] = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Import | ast.ImportFrom | ast.Global | ast.Nonlocal):
                return None
            if isinstance(sub, ast.Yield | ast.YieldFrom | ast.Await | ast.Lambda):
                return None
            if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef) and sub is not node:
                return None
            if (
                isinstance(sub, ast.Attribute)
                and isinstance(sub.value, ast.Call)
                and isinstance(sub.value.func, ast.Name)
                and sub.value.func.id == "super"
            ):
                if sub.attr not in allowed_super or sub.value.args or sub.value.keywords:
                    return None
                super_attributes.add(id(sub.value))
            if isinstance(sub, ast.Name):
                if isinstance(sub.ctx, ast.Store):
                    bound.add(sub.id)
                elif isinstance(sub.ctx, ast.Load):
                    loaded.add(sub.id)
            elif isinstance(sub, ast.arg):
                bound.add(sub.arg)
            elif isinstance(sub, ast.ExceptHandler) and sub.name:
                bound.add(sub.name)
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id == "super"
                and id(sub) not in super_attributes
            ):
                return None
        if self_name is not None:
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Attribute)
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id == self_name
                ):
                    if sub.attr in own:
                        if sub.attr not in carried and sub.attr not in queue:
                            queue.append(sub.attr)
                    else:
                        return None
            loaded.discard(self_name)
        for global_name in sorted(loaded - bound):
            if global_name in vars(builtins):
                continue
            if global_name not in module_globals:
                return None
            value = module_globals[global_name]
            if inspect.ismodule(value):
                if value is status_module:
                    imports[global_name] = ("__sanka_view_shim__", "status")
                elif value.__name__ in _VIEW_CARRYOVER_MODULES:
                    imports[global_name] = (value.__name__, None)
                else:
                    return None
            elif value is response_module.Response:
                imports[global_name] = ("__sanka_view_shim__", "Response")
            elif value is request_module.Request:
                imports[global_name] = ("__sanka_view_shim__", "Request")
            elif getattr(value, "__module__", None) == "typing":
                imports[global_name] = ("typing", str(getattr(value, "__name__", global_name)))
            else:
                return None
        carried[name] = source
        order.append(name)
    covered = list(operations)
    if "update" in covered and "partial_update" not in covered:
        covered.append("partial_update")
    return {
        "class_name": f"{view_class.__name__}Carryover",
        "operations": covered,
        "methods": [{"name": name, "source": carried[name]} for name in order],
        "imports": [[alias, module, attr] for alias, (module, attr) in sorted(imports.items())],
    }


def _view_auth_support(view_class: type[Any], model: Any) -> ViewAuthIR | None:
    """Capture the view's auth semantics, or None when outside the envelope.

    Recognized exactly: AllowAny (no enforcement, no perform_create override);
    or IsAuthenticated with DRF TokenAuthentication, optionally one
    owner-or-read-only object permission (matched structurally) and the
    ``serializer.save(field=self.request.user)`` perform_create idiom.
    """
    permissions_module = importlib.import_module("rest_framework.permissions")
    mixins = importlib.import_module("rest_framework.mixins")
    permissions = list(view_class.permission_classes)
    create_overridden = (
        getattr(view_class, "perform_create", mixins.CreateModelMixin.perform_create)
        is not mixins.CreateModelMixin.perform_create
    )
    if all(item is permissions_module.AllowAny for item in permissions):
        if create_overridden:
            return None
        return ViewAuthIR(require_authenticated=False)
    from sanka_code_migration.drf.access_contracts import (
        UnsupportedContract,
        capture_creator_membership,
        capture_membership_permission,
        user_identity,
    )

    membership_rules = [capture_membership_permission(item, model) for item in permissions]
    member_permission = next((item for item in membership_rules if item is not None), None)
    if permissions_module.IsAuthenticated not in permissions and member_permission is None:
        return None
    extras = [item for item in permissions if item is not permissions_module.IsAuthenticated]
    owner_field: str | None = None
    if len(extras) == 1:
        owner_field = _match_owner_permission(extras[0])
        if owner_field is None and member_permission is None:
            return None
    elif extras:
        return None
    authentication_module = importlib.import_module("rest_framework.authentication")
    authenticators = list(view_class.authentication_classes)
    if len(authenticators) != 1:
        return None
    if authenticators[0] is not authentication_module.TokenAuthentication:
        return None
    inject_owner: str | None = None
    if create_overridden:
        inject_owner = _match_perform_create(view_class)
        if inject_owner is None and capture_creator_membership(view_class, model) is None:
            return None
    owner_attname = _user_fk_attname(model, owner_field) if owner_field else None
    if owner_field is not None and owner_attname is None:
        return None
    inject_attname = _user_fk_attname(model, inject_owner) if inject_owner else None
    if inject_owner is not None and inject_attname is None:
        return None
    token_model = authenticators[0]().get_model()
    key_field = token_model._meta.pk
    try:
        user = user_identity()
    except UnsupportedContract:
        return None
    return ViewAuthIR(
        require_authenticated=permissions_module.IsAuthenticated in permissions,
        user=user,
        token_keyword=str(authenticators[0].keyword),
        token_db_table=str(token_model._meta.db_table),
        token_key_column=str(key_field.column),
        token_key_max_length=int(getattr(key_field, "max_length", None) or 40),
        token_user_column=str(token_model._meta.get_field("user").attname),
        owner_field=owner_field,
        owner_attname=owner_attname,
        inject_owner=inject_owner,
        inject_owner_attname=inject_attname,
        messages=_probe_auth_messages(authenticators[0]),
    )


def _user_fk_attname(model: Any, field_name: str | None) -> str | None:
    if model is None or field_name is None:
        return None
    exceptions_module = importlib.import_module("django.core.exceptions")
    try:
        field = model._meta.get_field(field_name)
    except exceptions_module.FieldDoesNotExist:
        return None
    auth_module = importlib.import_module("django.contrib.auth")
    if not getattr(field, "is_relation", False):
        return None
    if field.related_model is not auth_module.get_user_model():
        return None
    return str(field.attname)


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _attr_chain(node: ast.expr) -> list[str] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return list(reversed(parts))
    return None


def _match_owner_permission(perm_class: Any) -> str | None:
    """Return the owner field of an owner-or-read-only permission, or None.

    Matched structurally from the AST: ``has_object_permission`` must be the
    canonical safe-methods short-circuit plus an ownership comparison between
    an attribute of the object and the requesting user. Arbitrary permission
    logic cannot be regenerated honestly and keeps the view out of the
    envelope.
    """
    permissions_module = importlib.import_module("rest_framework.permissions")
    if not (
        inspect.isclass(perm_class) and issubclass(perm_class, permissions_module.BasePermission)
    ):
        return None
    permission_type: Any = perm_class
    if permission_type.has_permission is not permissions_module.BasePermission.has_permission:
        return None
    if (
        permission_type.has_object_permission
        is permissions_module.BasePermission.has_object_permission
    ):
        return None
    import textwrap

    try:
        source = textwrap.dedent(inspect.getsource(permission_type.has_object_permission))
    except (OSError, TypeError):
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    if not tree.body or not isinstance(tree.body[0], ast.FunctionDef):
        return None
    func = tree.body[0]
    arg_names = [item.arg for item in func.args.args]
    if len(arg_names) != 4:
        return None
    _, request_name, _, obj_name = arg_names
    body = [node for node in func.body if not _is_docstring(node)]
    if len(body) == 1 and isinstance(body[0], ast.Return):
        value = body[0].value
        if (
            isinstance(value, ast.BoolOp)
            and isinstance(value.op, ast.Or)
            and len(value.values) == 2
            and _is_safe_method_check(value.values[0], request_name)
        ):
            return _ownership_field(value.values[1], request_name, obj_name)
        return None
    if (
        len(body) == 2
        and isinstance(body[0], ast.If)
        and _is_safe_method_check(body[0].test, request_name)
        and not body[0].orelse
        and len(body[0].body) == 1
        and isinstance(body[0].body[0], ast.Return)
        and isinstance(body[0].body[0].value, ast.Constant)
        and body[0].body[0].value.value is True
        and isinstance(body[1], ast.Return)
        and body[1].value is not None
    ):
        return _ownership_field(body[1].value, request_name, obj_name)
    return None


def _is_safe_method_check(node: ast.expr, request_name: str) -> bool:
    if not (isinstance(node, ast.Compare) and len(node.ops) == 1):
        return False
    if not isinstance(node.ops[0], ast.In):
        return False
    left = _attr_chain(node.left)
    if left != [request_name, "method"]:
        return False
    target = _attr_chain(node.comparators[0])
    return target is not None and target[-1] == "SAFE_METHODS"


def _ownership_field(node: ast.expr | None, request_name: str, obj_name: str) -> str | None:
    if node is None:
        return None
    if not (
        isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq)
    ):
        return None
    left, right = node.left, node.comparators[0]
    for obj_side, user_side in ((left, right), (right, left)):
        field = _obj_owner_attr(obj_side, obj_name)
        if field is not None and _is_request_user(user_side, request_name):
            return field
    return None


def _obj_owner_attr(node: ast.expr, obj_name: str) -> str | None:
    chain = _attr_chain(node)
    if not chain or chain[0] != obj_name:
        return None
    if len(chain) == 2:
        name = chain[1]
        if name.endswith("_id") and len(name) > 3:
            return name[:-3]
        return name
    if len(chain) == 3 and chain[2] in ("id", "pk"):
        return chain[1]
    return None


def _is_request_user(node: ast.expr, request_name: str) -> bool:
    chain = _attr_chain(node)
    if not chain or len(chain) < 2 or chain[0] != request_name or chain[1] != "user":
        return False
    if len(chain) == 2:
        return True
    return len(chain) == 3 and chain[2] in ("id", "pk")


def _match_perform_create(view_class: type[Any]) -> str | None:
    """Return the injected kwarg of ``serializer.save(field=self.request.user)``."""
    import textwrap

    try:
        source = textwrap.dedent(inspect.getsource(view_class.perform_create))
    except (OSError, TypeError):
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    if not tree.body or not isinstance(tree.body[0], ast.FunctionDef):
        return None
    func = tree.body[0]
    arg_names = [item.arg for item in func.args.args]
    if len(arg_names) != 2:
        return None
    self_name, serializer_name = arg_names
    body = [node for node in func.body if not _is_docstring(node)]
    if len(body) != 1 or not isinstance(body[0], ast.Expr):
        return None
    call = body[0].value
    if not (isinstance(call, ast.Call) and not call.args and len(call.keywords) == 1):
        return None
    if _attr_chain(call.func) != [serializer_name, "save"]:
        return None
    keyword = call.keywords[0]
    if keyword.arg is None:
        return None
    if _attr_chain(keyword.value) != [self_name, "request", "user"]:
        return None
    return str(keyword.arg)


def _analyze_create_carryover(
    serializer_class: type[Any],
) -> tuple[str, tuple[tuple[str, str, str | None], ...]] | None:
    """Admit an overridden ``create()`` into the native envelope.

    Nested writes are regenerated as async SQL (parent row plus children),
    not re-emitted as Django. The author's ``create()`` still has to resolve
    only to the application's models, ``django.db.transaction``, or
    ``serializers.ValidationError`` — anything else stays outside the envelope.
    """
    import builtins
    import textwrap

    serializers_module = importlib.import_module("rest_framework.serializers")
    models_module = importlib.import_module("django.db.models")
    try:
        source = textwrap.dedent(inspect.getsource(serializer_class.create))
    except (OSError, TypeError):
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    if not tree.body or not isinstance(tree.body[0], ast.FunctionDef):
        return None
    func = tree.body[0]
    arg_names = [item.arg for item in func.args.args]
    if len(arg_names) != 2 or func.args.kwonlyargs or func.args.vararg or func.args.kwarg:
        return None
    self_name, data_name = arg_names

    bound: set[str] = {self_name, data_name}
    loaded: set[str] = set()
    attribute_uses: dict[str, set[str]] = {}
    for node in ast.walk(func):
        if isinstance(node, ast.Import | ast.ImportFrom):
            return None
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Store):
                bound.add(node.id)
            elif isinstance(node.ctx, ast.Load):
                loaded.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            attribute_uses.setdefault(node.value.id, set()).add(node.attr)
    if self_name in loaded:
        return None

    module_globals = vars(importlib.import_module(serializer_class.__module__))
    imports: list[tuple[str, str, str | None]] = []
    for name in sorted(loaded - bound):
        if (
            name in vars(builtins)
            and module_globals.get(name, vars(builtins)[name]) is vars(builtins)[name]
        ):
            continue
        if name not in module_globals:
            return None
        value = module_globals[name]
        if inspect.isclass(value) and issubclass(value, models_module.Model):
            imports.append((name, str(value.__module__), str(value.__qualname__)))
        elif inspect.ismodule(value) and value.__name__ == "django.db.transaction":
            imports.append((name, "django.db.transaction", None))
        elif value is serializers_module:
            if attribute_uses.get(name, set()) - {"ValidationError"}:
                return None
            imports.append((name, "__sanka_shim__", "serializers"))
        elif value is serializers_module.ValidationError:
            imports.append((name, "__sanka_shim__", "ValidationError"))
        else:
            return None
    return source, tuple(imports)


def _match_update_drop(serializer_class: type[Any]) -> tuple[str, ...] | None:
    """Match the drop-children update idiom, returning the dropped fields.

    Accepted shape: zero or more ``validated_data.pop("<field>"[, None])``
    statements followed by ``return super().update(instance, validated_data)``.
    """
    import textwrap

    try:
        source = textwrap.dedent(inspect.getsource(serializer_class.update))
    except (OSError, TypeError):
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    if not tree.body or not isinstance(tree.body[0], ast.FunctionDef):
        return None
    func = tree.body[0]
    arg_names = [item.arg for item in func.args.args]
    if len(arg_names) != 3:
        return None
    _, instance_name, data_name = arg_names
    body = [node for node in func.body if not _is_docstring(node)]
    if not body or not isinstance(body[-1], ast.Return):
        return None
    dropped: list[str] = []
    for statement in body[:-1]:
        if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)):
            return None
        call = statement.value
        if _attr_chain(call.func) != [data_name, "pop"] or call.keywords:
            return None
        if not call.args or not isinstance(call.args[0], ast.Constant):
            return None
        if not isinstance(call.args[0].value, str):
            return None
        if len(call.args) == 2:
            if not (isinstance(call.args[1], ast.Constant) and call.args[1].value is None):
                return None
        elif len(call.args) != 1:
            return None
        dropped.append(call.args[0].value)
    tail = body[-1].value
    if not (
        isinstance(tail, ast.Call)
        and not tail.keywords
        and isinstance(tail.func, ast.Attribute)
        and tail.func.attr == "update"
        and isinstance(tail.func.value, ast.Call)
        and isinstance(tail.func.value.func, ast.Name)
        and tail.func.value.func.id == "super"
        and not tail.func.value.args
        and len(tail.args) == 2
        and isinstance(tail.args[0], ast.Name)
        and tail.args[0].id == instance_name
        and isinstance(tail.args[1], ast.Name)
        and tail.args[1].id == data_name
    ):
        return None
    return tuple(dropped)


def _probe_auth_messages(auth_class: type[Any]) -> tuple[tuple[str, str], ...]:
    """Capture the auth error strings the live DRF installation produces.

    The header-parsing failures are probed with fake requests (no database
    access). The invalid-key and inactive-user paths require database rows a
    scan must never create, so those two strings are DRF's stable inline
    defaults.
    """
    exceptions_module = importlib.import_module("rest_framework.exceptions")
    authenticator = auth_class()
    keyword = str(auth_class.keyword)
    messages = {
        "no_credentials": str(exceptions_module.NotAuthenticated.default_detail),
        "forbidden": str(exceptions_module.PermissionDenied.default_detail),
        "invalid_token": "Invalid token.",
        "inactive_user": "User inactive or deleted.",
        "www_authenticate": str(authenticator.authenticate_header(None)),
    }

    class _ProbeRequest:
        def __init__(self, header: str) -> None:
            self.META = {"HTTP_AUTHORIZATION": header}
            self.headers = {"authorization": header}

    for key, header in (("empty_header", keyword), ("spaced_header", f"{keyword} a b")):
        try:
            authenticator.authenticate(_ProbeRequest(header))
        except exceptions_module.AuthenticationFailed as error:
            messages[key] = str(error.detail)
    return tuple(sorted(messages.items()))


def _api_root_links(callback: Any) -> tuple[tuple[str, str], ...] | None:
    initkwargs = getattr(callback, "view_initkwargs", None) or {}
    root_dict = initkwargs.get("api_root_dict") or {}
    if not root_dict:
        return None
    django_urls = importlib.import_module("django.urls")
    links: list[tuple[str, str]] = []
    for key, url_name in root_dict.items():
        try:
            links.append((str(key), str(django_urls.reverse(url_name))))
        except django_urls.NoReverseMatch:
            return None
    return tuple(sorted(links))


def _serializer_ir(view_class: type[Any], serializer_name: str) -> SerializerIR | None:
    serializers_module = importlib.import_module("rest_framework.serializers")
    serializer_class = view_class.serializer_class
    if not issubclass(serializer_class, serializers_module.ModelSerializer):
        return None
    queryset = view_class.queryset
    model = getattr(queryset, "model", None) or getattr(serializer_class.Meta, "model", None)
    if model is None:
        return None
    if queryset is None:
        from sanka_code_migration.drf.access_contracts import capture_query

        if capture_query(view_class, model) is None:
            return None
        queryset = model.objects.all()
    lookup = str(getattr(view_class, "lookup_field", "pk") or "pk")
    ir = _build_serializer_ir(
        serializer_class,
        model,
        name=serializer_name,
        ordering=tuple(
            str(item) for item in (queryset.query.order_by or model._meta.ordering or ())
        ),
        lookup=lookup,
        analyze_writes=True,
    )
    if queryset.query.where.children:
        ir = replace_dataclass(ir, supported=False)
    return ir


def _build_serializer_ir(
    serializer_class: type[Any],
    model: Any,
    *,
    name: str,
    ordering: tuple[str, ...],
    lookup: str = "pk",
    analyze_writes: bool,
) -> SerializerIR:
    """Build the IR for one ModelSerializer (top-level or nested child)."""
    serializers_module = importlib.import_module("rest_framework.serializers")
    supported = True
    if _defines_custom_validation(serializer_class):
        supported = False
    fields: list[SerializerFieldIR] = []
    has_writable_nested = False
    serializer_fields = serializer_class().fields
    for field_name, field in serializer_fields.items():
        field_ir = _serializer_field_ir(str(field_name), field, model)
        fields.append(field_ir)
        supported = supported and field_ir.supported
        if field_ir.kind == "nested_many" and not field_ir.read_only:
            has_writable_nested = True

    if model._meta.pk.get_internal_type() == "UUIDField" and not any(
        field.kind == "uuid"
        and field.read_only
        and (field.attname or field.name) == model._meta.pk.attname
        for field in fields
    ):
        # Do not infer an integer key or change Django primary-key mutation semantics.
        supported = False

    create_style = "default"
    create_source: str | None = None
    create_contract: dict[str, Any] | None = None
    create_imports: tuple[tuple[str, str, str | None], ...] = ()
    update_drops: tuple[str, ...] | None = None
    if analyze_writes:
        create_overridden = serializer_class.create is not serializers_module.ModelSerializer.create
        update_overridden = serializer_class.update is not serializers_module.ModelSerializer.update
        if create_overridden:
            from sanka_code_migration.drf.access_contracts import capture_parent_create

            parent_create = capture_parent_create(serializer_class, model)
            carryover = _analyze_create_carryover(serializer_class)
            if parent_create is not None:
                create_style = "contract"
                create_contract = parent_create
            elif carryover is None:
                supported = False
            else:
                create_style = "carryover"
                create_source, create_imports = carryover
                from sanka_code_migration.drf.nested_create import (
                    lower_nested_create,
                    supports_default_model_writes,
                )

                nested_fields = [
                    field for field in fields if field.kind == "nested_many" and not field.read_only
                ]
                if len(nested_fields) == 1 and nested_fields[0].child is not None:
                    nested_field = nested_fields[0]
                    child_ir = nested_field.child
                    assert child_ir is not None
                    child_model = serializer_fields[nested_field.name].child.Meta.model
                    nested_supported = (
                        nested_field.required
                        and not nested_field.allow_null
                        and not any(
                            field.kind == "nested_many" and not field.read_only
                            for field in child_ir.fields
                        )
                        and supports_default_model_writes(model, child_model)
                    )
                    create_contract = (
                        lower_nested_create(
                            create_source,
                            parent_aliases={
                                alias
                                for alias, module, attr in create_imports
                                if (module, attr) == (model.__module__, model.__qualname__)
                            },
                            child_aliases={
                                alias
                                for alias, module, attr in create_imports
                                if (module, attr) == (child_ir.model_module, child_ir.model_class)
                            },
                            transaction_aliases={
                                alias
                                for alias, module, _attr in create_imports
                                if module == "django.db.transaction"
                            },
                            error_aliases={
                                alias + ".ValidationError" if attr == "serializers" else alias
                                for alias, module, attr in create_imports
                                if module == "__sanka_shim__"
                            },
                            field=nested_field.name,
                            foreign_key=(nested_field.attname or "").removesuffix("_id"),
                            integer_fields={
                                (field.attname or field.name)
                                for field in child_ir.fields
                                if field.kind in {"integer", "big_integer"} and not field.read_only
                            },
                        )
                        if nested_supported
                        else None
                    )
                if create_contract is None:
                    supported = False
        elif has_writable_nested:
            # DRF's default create() raises on writable nested fields; an
            # honest native migration needs the author's own create logic.
            supported = False
        if update_overridden:
            update_drops = _match_update_drop(serializer_class)
            if update_drops is None:
                supported = False
        elif has_writable_nested:
            supported = False
    elif serializer_class.create is not serializers_module.ModelSerializer.create or (
        serializer_class.update is not serializers_module.ModelSerializer.update
    ):
        # Nested children are written by the parent's create path; their own
        # overrides would be silently skipped, so they are unsupported.
        supported = False

    storage = []
    for model_field in model._meta.concrete_fields:
        if getattr(model_field, "auto_now", False):
            if (
                model_field.get_internal_type() != "DateTimeField"
                or not importlib.import_module("django.conf").settings.USE_TZ
            ):
                supported = False
            else:
                storage.append(
                    {"name": str(model_field.attname), "kind": "datetime", "auto_now": True}
                )
    return SerializerIR(
        storage=tuple(storage),
        name=name,
        model=f"{model.__module__}.{model.__qualname__}",
        model_module=str(model.__module__),
        model_class=str(model.__qualname__),
        object_name=str(model._meta.object_name),
        db_table=str(model._meta.db_table),
        pk_attname=str(model._meta.pk.attname),
        ordering=ordering,
        lookup=lookup,
        fields=tuple(fields),
        create_style=create_style,
        create_source=create_source,
        create_contract=create_contract,
        create_imports=create_imports,
        update_drops=update_drops,
        supported=supported,
    )


def _nested_many_field_ir(name: str, field: Any, parent_model: Any) -> SerializerFieldIR:
    """IR for a ``many=True`` nested ModelSerializer child."""
    serializers_module = importlib.import_module("rest_framework.serializers")
    child = field.child
    if not isinstance(child, serializers_module.ModelSerializer):
        return SerializerFieldIR(name=name, kind="unsupported", supported=False)
    if getattr(field, "source", name) != name or not getattr(field, "allow_empty", True):
        return SerializerFieldIR(name=name, kind="unsupported", supported=False)
    child_class = type(child)
    child_model = getattr(getattr(child_class, "Meta", None), "model", None)
    if child_model is None or parent_model is None:
        return SerializerFieldIR(name=name, kind="unsupported", supported=False)
    try:
        relation = parent_model._meta.get_field(name)
    except Exception:
        return SerializerFieldIR(name=name, kind="unsupported", supported=False)
    if getattr(relation, "related_model", None) is not child_model:
        return SerializerFieldIR(name=name, kind="unsupported", supported=False)
    child_model_type: Any = child_model
    child_ir = _build_serializer_ir(
        child_class,
        child_model_type,
        name=f"{child_class.__module__}.{child_class.__qualname__}",
        ordering=tuple(str(item) for item in (child_model_type._meta.ordering or ())),
        analyze_writes=False,
    )
    if getattr(relation, "many_to_many", False) and field.read_only:
        from sanka_code_migration.drf.access_contracts import (
            UnsupportedContract,
            membership,
            user_identity,
        )

        try:
            member = {**membership(parent_model, [name]), "user": user_identity()}
        except UnsupportedContract:
            return SerializerFieldIR(name=name, kind="unsupported", supported=False)
        if _defines_methods(child_class, serializers_module.ModelSerializer):
            return SerializerFieldIR(name=name, kind="unsupported", supported=False)
        types = importlib.import_module("rest_framework.fields")
        output_fields = []
        for key, child_field in child.fields.items():
            if (
                type(child_field) not in (types.IntegerField, types.CharField)
                or child_field.source != key
                or child_field.write_only
            ):
                return SerializerFieldIR(name=name, kind="unsupported", supported=False)
            model_field = child_model_type._meta.get_field(key)
            if model_field.is_relation or not model_field.concrete:
                return SerializerFieldIR(name=name, kind="unsupported", supported=False)
            output_fields.append({"name": key, "column": str(model_field.column)})
        member["output_fields"] = output_fields
        if child_model_type._meta.ordering:
            return SerializerFieldIR(name=name, kind="unsupported", supported=False)
        return SerializerFieldIR(name=name, kind="membership_read", read_only=True, relation=member)
    parent_fk = relation.field.attname if hasattr(relation, "field") else None
    if parent_fk is None:
        return SerializerFieldIR(name=name, kind="unsupported", supported=False)
    messages = {
        "required": str(field.error_messages.get("required", "")),
        "null": str(field.error_messages.get("null", "")),
        "not_a_list": str(field.error_messages.get("not_a_list", "")),
    }
    # The generated runtime only enforces uniqueness on top-level fields.
    child_supported = child_ir.supported and not any(item.unique for item in child_ir.fields)
    return SerializerFieldIR(
        name=name,
        kind="nested_many",
        required=bool(field.required),
        read_only=bool(field.read_only),
        allow_null=bool(field.allow_null),
        attname=str(parent_fk),
        child=child_ir,
        messages=tuple(sorted((key, value) for key, value in messages.items() if value)),
        supported=child_supported,
    )


def _status_codes() -> dict[str, int]:
    """DRF's HTTP_* status constants, captured so carried view code sees the same names."""
    status_module = importlib.import_module("rest_framework.status")
    return {
        name: int(getattr(status_module, name))
        for name in dir(status_module)
        if name.startswith("HTTP_") and isinstance(getattr(status_module, name), int)
    }


def _generic_messages() -> tuple[tuple[str, str], ...]:
    """Capture framework-level error strings from the live DRF installation."""
    exceptions_module = importlib.import_module("rest_framework.exceptions")
    return (
        ("method_not_allowed", str(exceptions_module.MethodNotAllowed.default_detail)),
        ("not_found", str(exceptions_module.NotFound.default_detail)),
    )


def _defines_custom_validation(cls: type[Any]) -> bool:
    for klass in cls.__mro__:
        if klass.__module__.startswith("rest_framework."):
            continue
        for name in vars(klass):
            if name == "validate" or name.startswith("validate_"):
                return True
    return False


def standard_field_validators(field: Any) -> bool:
    """The scalar IR represents stock field validators, not additional validator runs."""
    unique_validator = importlib.import_module("rest_framework.validators").UniqueValidator
    observed = [item for item in field.validators if type(item) is not unique_validator]
    expected = type(field)(
        *field._args, **{key: value for key, value in field._kwargs.items() if key != "validators"}
    ).validators
    if len(observed) != len(expected):
        return False
    return all(
        type(actual) is type(stock)
        and not callable(getattr(actual, "limit_value", None))
        and getattr(actual, "limit_value", None) == getattr(stock, "limit_value", None)
        and str(getattr(actual, "message", "")) == str(getattr(stock, "message", ""))
        and getattr(actual, "code", None) == getattr(stock, "code", None)
        for actual, stock in zip(observed, expected, strict=True)
    )


def _serializer_field_ir(name: str, field: Any, model: Any) -> SerializerFieldIR:
    fields_module = importlib.import_module("rest_framework.fields")
    relations_module = importlib.import_module("rest_framework.relations")
    validators_module = importlib.import_module("django.core.validators")
    if type(field) is relations_module.PrimaryKeyRelatedField and field.read_only:
        attname = _related_attname(model, name)
        model_field = model._meta.get_field(name) if attname else None
        target_field = getattr(model_field, "target_field", None)
        related_uuid = target_field is not None and target_field.get_internal_type() == "UUIDField"
        return SerializerFieldIR(
            name=name,
            kind="related_uuid" if related_uuid else "related_pk",
            read_only=True,
            attname=attname,
            supported=attname is not None and (not related_uuid or field.pk_field is None),
            allow_null=bool(field.allow_null),
        )
    serializers_module = importlib.import_module("rest_framework.serializers")
    if type(field) is serializers_module.ListSerializer:
        return _nested_many_field_ir(name, field, model)
    if type(field) is serializers_module.SerializerMethodField:
        from sanka_code_migration.drf.access_contracts import capture_computed_preview

        preview = capture_computed_preview(field, model)
        return SerializerFieldIR(
            name=name,
            kind="related_preview",
            read_only=True,
            supported=preview is not None,
            relation=preview or {},
        )
    kind: str | None = None
    if type(field) is fields_module.IntegerField:
        kind = "integer"
    elif type(field) is getattr(fields_module, "BigIntegerField", None):
        kind = "big_integer"
    elif type(field) is fields_module.BooleanField:
        kind = "boolean"
    elif type(field) is fields_module.UUIDField:
        if field.uuid_format != "hex_verbose":
            return SerializerFieldIR(name=name, kind="unsupported", supported=False)
        kind = "uuid"
    elif type(field) is fields_module.CharField:
        kind = "char"
    elif type(field) is fields_module.DecimalField:
        kind = "decimal"
        if (
            not getattr(field, "coerce_to_string", True)
            or field.rounding is not None
            or field.localize
            or field.normalize_output
        ):
            return SerializerFieldIR(name=name, kind="unsupported", supported=False)
    elif type(field) is fields_module.ChoiceField:
        kind = "choice"
        values = tuple(field.choices)
        if not all(isinstance(value, str | int) for value in values):
            return SerializerFieldIR(name=name, kind="unsupported", supported=False)
    elif type(field) is fields_module.DateTimeField:
        kind = "datetime"
        api_settings = importlib.import_module("rest_framework.settings").api_settings
        output_format = getattr(field, "format", api_settings.DATETIME_FORMAT)
        input_formats = getattr(field, "input_formats", api_settings.DATETIME_INPUT_FORMATS)
        iso = str(fields_module.ISO_8601).lower()
        if str(output_format).lower() != iso or [str(item).lower() for item in input_formats] != [
            iso
        ]:
            return SerializerFieldIR(name=name, kind="unsupported", supported=False)
    if kind is None:
        return SerializerFieldIR(name=name, kind="unsupported", supported=False)
    drf_validators = importlib.import_module("rest_framework.validators")
    allowed_validators: tuple[Any, ...] = (
        validators_module.MaxLengthValidator,
        validators_module.MinLengthValidator,
        validators_module.MaxValueValidator,
        validators_module.MinValueValidator,
        validators_module.ProhibitNullCharactersValidator,
        drf_validators.ProhibitSurrogateCharactersValidator,
        drf_validators.UniqueValidator,
    )
    if kind in {"boolean", "uuid"}:
        allowed_validators = (drf_validators.UniqueValidator,)
    supported = all(type(item) in allowed_validators for item in field.validators) and (
        standard_field_validators(field)
    )
    if any(
        getattr(field, key, None) is not None and _maybe_int(getattr(field, key)) is None
        for key in ("min_value", "max_value")
    ):
        supported = False
    unique = False
    unique_message: str | None = None
    for item in field.validators:
        if isinstance(item, drf_validators.UniqueValidator):
            unique = True
            unique_message = str(item.message)
    uuid_default = False
    if kind in {"uuid", "boolean"} and model is not None:
        import uuid

        try:
            model_field = model._meta.get_field(name)
        except importlib.import_module("django.core.exceptions").FieldDoesNotExist:
            return SerializerFieldIR(name=name, kind="unsupported", supported=False)
        expected_type = "UUIDField" if kind == "uuid" else "BooleanField"
        if model_field.get_internal_type() != expected_type or (
            kind == "boolean" and model_field.primary_key
        ):
            supported = False
        uuid_default = kind == "uuid" and model_field.default is uuid.uuid4
        if model_field.has_default() and callable(model_field.default) and not uuid_default:
            supported = False
    default = getattr(field, "default", fields_module.empty)
    has_default = default is not fields_module.empty
    default_on_create_only = False
    if not has_default:
        model_default = _django_field_default(model, name)
        if model_default is not fields_module.empty:
            default = model_default
            has_default = True
            default_on_create_only = kind in {"uuid", "boolean"}
    if kind == "uuid" and has_default:
        import uuid

        if isinstance(default, uuid.UUID):
            default = str(default)
    if has_default and not isinstance(default, str | int | float | bool | type(None)):
        supported = False
        default = None
    return SerializerFieldIR(
        name=name,
        kind=kind,
        required=bool(field.required),
        read_only=bool(field.read_only),
        allow_null=bool(field.allow_null),
        allow_blank=bool(getattr(field, "allow_blank", False)),
        trim_whitespace=bool(getattr(field, "trim_whitespace", True)),
        max_length=_maybe_int(getattr(field, "max_length", None)),
        min_length=_maybe_int(getattr(field, "min_length", None)),
        min_value=_maybe_int(getattr(field, "min_value", None)),
        max_value=_maybe_int(getattr(field, "max_value", None)),
        max_digits=_maybe_int(getattr(field, "max_digits", None)),
        decimal_places=_maybe_int(getattr(field, "decimal_places", None)),
        choices=tuple(field.choices) if kind == "choice" else (),
        has_default=has_default,
        default=default if has_default else None,
        unique=unique,
        unique_message=unique_message,
        messages=_field_messages(field, "integer" if kind == "big_integer" else kind),
        uuid_default=uuid_default,
        default_on_create_only=default_on_create_only,
        coerce_to_string=(
            bool(
                getattr(
                    field,
                    "coerce_to_string",
                    importlib.import_module(
                        "rest_framework.settings"
                    ).api_settings.COERCE_BIGINT_TO_STRING,
                )
            )
            if kind == "big_integer"
            else False
        ),
        supported=supported,
        timezone=_field_timezone_name(field) if kind == "datetime" else None,
    )


def _django_field_default(model: Any, name: str) -> Any:
    fields_module = importlib.import_module("rest_framework.fields")
    if model is None:
        return fields_module.empty
    exceptions_module = importlib.import_module("django.core.exceptions")
    try:
        field = model._meta.get_field(name)
    except exceptions_module.FieldDoesNotExist:
        return fields_module.empty
    if not field.has_default() or callable(field.default):
        return fields_module.empty
    return field.default


def _related_attname(model: Any, field_name: str) -> str | None:
    if model is None:
        return None
    exceptions_module = importlib.import_module("django.core.exceptions")
    try:
        field = model._meta.get_field(field_name)
    except exceptions_module.FieldDoesNotExist:
        return None
    if not getattr(field, "is_relation", False):
        return None
    return str(field.attname)


def _maybe_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


_MESSAGE_KEYS = {
    "uuid": ("required", "null", "invalid"),
    "boolean": ("required", "null", "invalid"),
    "integer": ("required", "null", "invalid", "min_value", "max_value", "max_string_length"),
    "decimal": (
        "required",
        "null",
        "invalid",
        "max_digits",
        "max_decimal_places",
        "max_whole_digits",
        "max_string_length",
    ),
    "choice": ("required", "null", "invalid_choice"),
    "datetime": ("required", "null", "invalid", "date", "make_aware", "overflow"),
    "char": (
        "required",
        "null",
        "invalid",
        "blank",
        "max_length",
        "min_length",
        "null_characters",
        "surrogate_characters",
    ),
}


def _field_messages(field: Any, kind: str) -> tuple[tuple[str, str], ...]:
    """Render the exact error strings DRF would emit for this field."""
    validators_module = importlib.import_module("django.core.validators")
    params = {
        key: value
        for key in ("min_value", "max_value", "max_length", "min_length", "max_digits")
        if (value := getattr(field, key, None)) is not None
    }
    if getattr(field, "decimal_places", None) is not None:
        params["max_decimal_places"] = field.decimal_places
        if getattr(field, "max_digits", None) is not None:
            params["max_whole_digits"] = field.max_digits - field.decimal_places
    if kind == "datetime":
        humanize = importlib.import_module("rest_framework.utils.humanize_datetime")
        api_settings = importlib.import_module("rest_framework.settings").api_settings
        input_formats = getattr(field, "input_formats", api_settings.DATETIME_INPUT_FORMATS)
        params["format"] = humanize.datetime_formats(input_formats)
    rendered: dict[str, str] = {}
    for key in _MESSAGE_KEYS[kind]:
        template = str(field.error_messages.get(key, ""))
        if not template:
            continue
        try:
            rendered[key] = template.format(**params)
        except (IndexError, KeyError):
            rendered[key] = template
    drf_validators = importlib.import_module("rest_framework.validators")
    for validator in getattr(field, "validators", ()):
        if isinstance(validator, validators_module.ProhibitNullCharactersValidator):
            rendered["null_characters"] = str(validator.message)
        elif isinstance(validator, drf_validators.ProhibitSurrogateCharactersValidator):
            rendered["surrogate_characters"] = str(validator.message)
    return tuple(sorted(rendered.items()))


def _route_methods(view_class: type[Any], actions: dict[str, str] | None) -> list[tuple[str, str]]:
    if actions:
        return sorted((method.upper(), action) for method, action in actions.items())
    methods: list[tuple[str, str]] = []
    for method in getattr(view_class, "http_method_names", ()):
        if method in {"options", "head", "trace"}:
            continue
        if callable(getattr(view_class, method, None)):
            methods.append((method.upper(), method))
    return methods


def _replace_named_regex_groups(value: str) -> tuple[str, bool, dict[str, str]]:
    """Replace balanced Django named groups without truncating nested regexes."""
    output: list[str] = []
    groups: dict[str, str] = {}
    supported = True
    index = 0
    while index < len(value):
        if not value.startswith("(?P<", index):
            output.append(value[index])
            index += 1
            continue
        name_end = value.find(">", index + 4)
        if name_end < 0:
            return value, False, {}
        name = value[index + 4 : name_end]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            return value, False, {}
        depth = 1
        cursor = name_end + 1
        group_start = cursor
        escaped = False
        in_class = False
        while cursor < len(value):
            char = value[cursor]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif in_class:
                if char == "]":
                    in_class = False
            elif char == "[":
                in_class = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    break
            cursor += 1
        if depth != 0:
            return value, False, {}
        expression = value[group_start:cursor]
        groups[name] = expression
        supported = supported and _regex_is_single_path_segment(expression)
        output.append(f"{{{name}}}")
        index = cursor + 1
    return "".join(output), supported, groups


def _regex_is_single_path_segment(expression: str) -> bool:
    """Conservatively prove a regex cannot consume a slash."""
    if not expression:
        return False
    index = 0
    while index < len(expression):
        char = expression[index]
        if char == "\\":
            if index + 1 >= len(expression):
                return False
            escaped = expression[index + 1]
            if escaped == "/" or escaped in {"D", "S", "W"} or escaped.isdigit():
                return False
            index += 2
            continue
        if char == "/" or char == ".":
            return False
        if char == "[":
            cursor = index + 1
            negated = cursor < len(expression) and expression[cursor] == "^"
            if negated:
                cursor += 1
            has_slash = False
            while cursor < len(expression) and expression[cursor] != "]":
                if expression[cursor] == "\\":
                    if cursor + 1 >= len(expression):
                        return False
                    escaped = expression[cursor + 1]
                    if escaped in {"D", "S", "W"} or escaped.isdigit():
                        return False
                    if escaped == "/":
                        has_slash = True
                    cursor += 2
                    continue
                if expression[cursor] == "/":
                    has_slash = True
                cursor += 1
            if cursor >= len(expression):
                return False
            if (negated and not has_slash) or (not negated and has_slash):
                return False
            index = cursor + 1
            continue
        if expression.startswith("(?", index) and not expression.startswith("(?:", index):
            return False
        index += 1
    return True


def _to_fastapi_path(raw: str) -> tuple[str, bool]:
    value = raw.strip()
    value = value.removesuffix("$").removesuffix(r"\Z")
    value = re.sub(r"(^|/)\^", r"\1", value)
    value, named_groups_supported, _groups = _replace_named_regex_groups(value)
    value = re.sub(
        r"<(?:(?:str|int|slug|uuid|path):)?([A-Za-z_][A-Za-z0-9_]*)>",
        r"{\1}",
        value,
    )
    value = value.replace(r"\/", "/").replace(r"\.", ".")
    value = value.replace("/?", "/")
    value = re.sub(r"\(\?:([^()]+)\)", r"\1", value)
    supported = named_groups_supported and re.search(r"[\[\]()+*?|\\^$]", value) is None
    path = "/" + value.lstrip("/")
    path = re.sub(r"/{2,}", "/", path)
    return path, supported


def _source_location(view_class: type[Any], root: Path) -> tuple[str | None, int | None]:
    try:
        path = Path(inspect.getsourcefile(view_class) or "").resolve()
        relative = str(path.relative_to(root))
        _, line = inspect.getsourcelines(view_class)
        return relative, line
    except (OSError, TypeError, ValueError):
        return None, None


def _qualified_name(value: Any) -> str | None:
    if value is None:
        return None
    module = getattr(value, "__module__", None)
    name = getattr(value, "__qualname__", None) or getattr(value, "__name__", None)
    if module and name:
        return f"{module}.{name}"
    return str(value)


def _safe_source(value: Any) -> str:
    try:
        return inspect.getsource(value)
    except (OSError, TypeError):
        return ""


def _count_test_files(root: Path) -> int:
    ignored = {".git", ".sanka", ".tox", ".venv", "node_modules", "site-packages", "venv"}
    return sum(
        1
        for path in root.rglob("*.py")
        if not ignored.intersection(path.relative_to(root).parts)
        if path.name.startswith("test_") or path.name.endswith("_test.py")
    )


def _artifact_path(root: Path, artifact_dir: str | Path, name: str) -> Path:
    directory = Path(artifact_dir)
    if not directory.is_absolute():
        directory = root / directory
    return directory / name


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise FrameworkMigrationError(f"{label} artifact not found: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise FrameworkMigrationError(f"could not read {label} artifact: {path}") from error
    if not isinstance(value, dict):
        raise FrameworkMigrationError(f"{label} artifact must be a JSON object: {path}")
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

# SPDX-License-Identifier: Apache-2.0
"""Capture stock ModelViewSet CRUD without importing DRF into generated apps."""

from __future__ import annotations

import ast
import inspect
import textwrap
from typing import Any

from sanka_code_migration.drf.scan import standard_field_validators

from .native import _function, isolated_module


def model_serializer(cls: Any, name: str) -> str | None:
    from django.db import transaction  # type: ignore[import-untyped]
    from django.db.models import Model  # type: ignore[import-untyped]
    from rest_framework import serializers  # type: ignore[import-untyped]
    from rest_framework.settings import api_settings  # type: ignore[import-untyped]
    from rest_framework.validators import UniqueValidator  # type: ignore[import-untyped]

    if cls.__bases__ != (serializers.ModelSerializer,):
        return None
    if set(vars(cls)) - {
        "__module__",
        "__doc__",
        "__firstlineno__",
        "__static_attributes__",
        "_declared_fields",
        "Meta",
        "create",
        "update",
    }:
        return None
    if set(vars(cls.Meta)) - {
        "__module__",
        "__doc__",
        "__dict__",
        "__weakref__",
        "__firstlineno__",
        "__static_attributes__",
        "model",
        "fields",
        "read_only_fields",
    }:
        return None
    instance = cls()
    if instance.validators or not isolated_module(inspect.getmodule(cls.Meta.model)):
        return None
    imports = [
        f"from {cls.Meta.model.__module__} import {cls.Meta.model.__name__}",
        "from sanka_model import ModelInput",
        "from django.db import transaction",
    ]
    fields, children = {}, []
    for key, field in instance.fields.items():
        if field.source != key or field.write_only or field.default is not serializers.empty:
            return None
        allowed = (
            serializers.CharField,
            serializers.IntegerField,
            serializers.DecimalField,
            serializers.ChoiceField,
            serializers.ListSerializer,
            getattr(serializers, "BigIntegerField", serializers.IntegerField),
        )
        if type(field) not in allowed:
            return None
        if type(field) is not serializers.ListSerializer and not standard_field_validators(
            field, cls.Meta.model
        ):
            return None
        spec = {
            "kind": type(field).__name__,
            "required": field.required,
            "read_only": field.read_only,
            "allow_null": field.allow_null,
            "messages": {k: str(v) for k, v in field.error_messages.items()},
        }
        if isinstance(field, serializers.IntegerField):
            spec["kind"] = "IntegerField"
        for attr in (
            "allow_blank",
            "trim_whitespace",
            "min_length",
            "max_length",
            "min_value",
            "max_value",
            "max_digits",
            "decimal_places",
            "max_whole_digits",
        ):
            if hasattr(field, attr):
                value = getattr(field, attr)
                spec[attr] = (
                    value
                    if value is None or isinstance(value, (bool, int, float, str))
                    else str(value)
                )
        if type(field) is serializers.ListSerializer:
            if (
                field.__class__ is not serializers.ListSerializer
                or field.min_length is not None
                or field.max_length is not None
            ):
                return None
            child_name = name + "_" + key
            child = model_serializer(type(field.child), child_name)
            if child is None:
                return None
            children.append(child)

            class ErrorProbe(serializers.Serializer):  # type: ignore[misc]
                value = serializers.IntegerField()

            probe = ErrorProbe(many=True, data=[{}])
            probe.is_valid()
            spec.update(
                kind="nested",
                allow_empty=field.allow_empty,
                serializer=child_name,
                indexed_errors=isinstance(probe.errors, dict),
            )
        if type(field) is serializers.ChoiceField:
            spec["choices"] = list(field.choices)
        if type(field) is serializers.DecimalField:
            if (
                getattr(field, "coerce_to_string", api_settings.COERCE_DECIMAL_TO_STRING)
                is not True
            ):
                return None
            if field.rounding is not None or field.localize or field.normalize_output:
                return None
        for validator in field.validators:
            if type(validator) is UniqueValidator:
                spec["unique"] = str(validator.message)
            elif type(validator).__module__ not in {
                "django.core.validators",
                "rest_framework.validators",
            } or type(validator).__name__ not in {
                "MaxLengthValidator",
                "MinLengthValidator",
                "MaxValueValidator",
                "MinValueValidator",
                "ProhibitNullCharactersValidator",
                "ProhibitSurrogateCharactersValidator",
            }:
                return None
        fields[key] = spec
    methods = []
    for method in ("create", "update"):
        if method not in vars(cls):
            continue
        function = vars(cls)[method]
        globals_allowed = {"sum", "super"}
        if globals_allowed & function.__globals__.keys():
            return None
        for key, value in function.__globals__.items():
            if value is serializers:
                imports.append(f"from sanka_native import _exceptions as {key}")
                globals_allowed.add(key)
            elif value is transaction:
                globals_allowed.add(key)
                imports.append(f"from django.db import transaction as {key}")
            elif inspect.isclass(value) and issubclass(value, Model):
                if not isolated_module(inspect.getmodule(value)):
                    return None
                imports.append(f"from {value.__module__} import {value.__name__} as {key}")
                globals_allowed.add(key)
        node = _function(function, globals_allowed)
        if node is None:
            return None
        expected = (
            ["self", "validated_data"]
            if method == "create"
            else ["self", "instance", "validated_data"]
        )
        if [a.arg for a in node.args.args] != expected:
            return None
        for item in ast.walk(node):
            if (
                isinstance(item, ast.Attribute)
                and isinstance(item.value, ast.Name)
                and function.__globals__.get(item.value.id) is serializers
                and item.attr != "ValidationError"
            ):
                return None
        methods.append(textwrap.indent(ast.unparse(node), "    "))
    if any(f["kind"] == "nested" and not f["read_only"] for f in fields.values()) and (
        "create" not in vars(cls) or "update" not in vars(cls)
    ):
        return None
    result = "\n".join(imports + children)
    result += (
        f"\nclass {name}(ModelInput):\n    model = {cls.Meta.model.__name__}\n"
        f"    fields = {fields!r}\n"
    )
    result += f"    non_field_errors = {api_settings.NON_FIELD_ERRORS_KEY!r}\n"
    result += f"    messages = { {k: str(v) for k, v in instance.error_messages.items()}!r}\n"
    result += "\n".join(methods) + "\n"
    for key, field in fields.items():
        if field["kind"] == "nested":
            result += f"{name}.fields[{key!r}]['serializer'] = {field['serializer']}\n"
    return result


def handler(
    view: Any, action: str, method: str, initialization: dict[str, Any]
) -> tuple[str | None, str]:
    from django.conf import settings  # type: ignore[import-untyped]
    from rest_framework import viewsets
    from rest_framework.authentication import (  # type: ignore[import-untyped]
        BasicAuthentication,
        SessionAuthentication,
    )
    from rest_framework.metadata import SimpleMetadata  # type: ignore[import-untyped]
    from rest_framework.parsers import (  # type: ignore[import-untyped]
        FormParser,
        JSONParser,
        MultiPartParser,
    )
    from rest_framework.permissions import AllowAny  # type: ignore[import-untyped]
    from rest_framework.renderers import (  # type: ignore[import-untyped]
        BrowsableAPIRenderer,
        JSONRenderer,
    )

    if not default_settings():
        return None, "custom REST framework settings need adaptation"
    if not issubclass(view, viewsets.ModelViewSet):
        return None, "not a stock ModelViewSet"
    if view.__bases__ != (viewsets.ModelViewSet,):
        return None, "custom ViewSet inheritance needs adaptation"
    allowed = {
        "__module__",
        "__doc__",
        "__firstlineno__",
        "__static_attributes__",
        "queryset",
        "serializer_class",
        "authentication_classes",
        "permission_classes",
        "renderer_classes",
        "parser_classes",
        "name",
        "description",
        "suffix",
        "detail",
        "basename",
        "__annotations__",
    }
    if set(vars(view)) - allowed:
        return None, "custom ViewSet behavior needs adaptation"
    if (
        view.permission_classes != [AllowAny]
        or view.throttle_classes
        or view.filter_backends
        or view.pagination_class
    ):
        return None, "permissions, throttling, filtering or pagination need adaptation"
    if view.renderer_classes not in (
        [JSONRenderer],
        [JSONRenderer, BrowsableAPIRenderer],
    ) or view.parser_classes not in ([JSONParser], [JSONParser, FormParser, MultiPartParser]):
        return None, "ModelViewSet requires an explicit JSON renderer and parser"
    if view.authentication_classes not in ([], [SessionAuthentication, BasicAuthentication]):
        return None, "custom ViewSet authentication needs adaptation"
    if settings.MIDDLEWARE or view.metadata_class is not SimpleMetadata:
        return None, "middleware or custom metadata needs adaptation"
    if view.authentication_classes and settings.AUTHENTICATION_BACKENDS != [
        "django.contrib.auth.backends.ModelBackend"
    ]:
        return None, "custom authentication backends need adaptation"
    query = view.queryset
    if query is None or str(query.query) != str(query.model._default_manager.all().query):
        return None, "custom queryset needs adaptation"
    serializer = model_serializer(view.serializer_class, "_Serializer")
    if serializer is None or query.model is not view.serializer_class.Meta.model:
        return None, "serializer behavior needs adaptation"
    body = [
        "def route(pk=None, format=None):",
        textwrap.indent(serializer, "    "),
        "    if format not in (None, 'json'):",
        "        raise _RequestError('Not found.', 404)",
        "    _negotiate_json('format')",
    ]
    if view.authentication_classes:
        body += [
            "    from sanka_model import authenticate_basic",
            "    authenticate_basic(request)",
        ]
    if method == "OPTIONS":
        metadata = {
            "name": view(**initialization).get_view_name(),
            "description": view(**initialization).get_view_description(),
            "renders": ["application/json"],
            "parses": ["application/json"],
        }
        info = SimpleMetadata().get_serializer_info(view.serializer_class())
        # Detail/list identity is known from the router action map, passed below.
        metadata["actions"] = {"PUT" if action == "retrieve" else "POST": info}
        body.append(f"    metadata = {dict(metadata)!r}")
        if action == "retrieve":
            body += [
                "    try:",
                "        _Serializer.model._default_manager.get(pk=pk)",
                "    except (_Serializer.model.DoesNotExist, ValueError, TypeError):",
                "        metadata.pop('actions', None)",
            ]
        body.append("    return Response(metadata)")
    elif action == "list":
        body.append(
            "    return Response([_Serializer(item).data for item in _Serializ"
            "er.model._default_manager.all()])"
        )
    else:
        if action != "create":
            body += [
                "    try:",
                "        instance = _Serializer.model._default_manager.get(pk=pk)",
                "    except (_Serializer.model.DoesNotExist, ValueError, TypeError):",
                "        raise _RequestError('Not found.', 404)",
            ]
        if action == "retrieve":
            body.append("    return Response(_Serializer(instance).data)")
        elif action == "destroy":
            body += ["    instance.delete()", "    return Response(status=204)"]
        elif action in {"create", "update", "partial_update"}:
            body += [
                (
                    f"    serializer = _Serializer({'' if action == 'create' else 'instance, '}"
                    f"data=_json_data(), partial={action == 'partial_update'})"
                ),
                "    serializer.is_valid(raise_exception=True)",
                "    serializer.save()",
                (
                    "    return Response(serializer.data, "
                    f"status={201 if action == 'create' else 200})"
                ),
            ]
        else:
            return None, "custom router action needs adaptation"
    return "\n".join(body), ""


def root_handler(callback: Any, method: str) -> tuple[str | None, str]:
    from django.urls import reverse  # type: ignore[import-untyped]
    from rest_framework.authentication import BasicAuthentication, SessionAuthentication
    from rest_framework.permissions import AllowAny
    from rest_framework.routers import APIRootView  # type: ignore[import-untyped]

    view = callback.cls
    if (
        not default_settings()
        or view is not APIRootView
        or view.permission_classes != [AllowAny]
        or view.authentication_classes not in ([], [SessionAuthentication, BasicAuthentication])
    ):
        return None, "custom API root needs adaptation"
    paths = {key: reverse(value) for key, value in callback.initkwargs["api_root_dict"].items()}
    lines = [
        "def route(format=None):",
        "    _negotiate_json('format')",
        "    if format not in (None, 'json'):",
        "        raise _RequestError('Not found.', 404)",
    ]
    if view.authentication_classes:
        lines += [
            "    from sanka_model import authenticate_basic",
            "    authenticate_basic(request)",
        ]
    if method == "OPTIONS":
        lines.append(
            "    return Response({'name':'Api Root','description':'The default"
            " basic root view for DefaultRouter', 'renders':['application/json"
            "'],'parses':['application/json']})"
        )
    else:
        lines += [
            f"    paths = {paths!r}",
            (
                "    return Response({key: request.host_url.rstrip('/') + (path if"
                " format is None else path.rstrip('/') + '.json') for key, path in"
                " paths.items()})"
            ),
        ]
    return "\n".join(lines), ""


def default_settings() -> bool:
    from django.conf import settings

    return (
        not settings.MIDDLEWARE
        and settings.AUTHENTICATION_BACKENDS == ["django.contrib.auth.backends.ModelBackend"]
        and not (
            set(getattr(settings, "REST_FRAMEWORK", {}))
            - {
                "UNAUTHENTICATED_USER",
                "DEFAULT_AUTHENTICATION_CLASSES",
                "DEFAULT_PERMISSION_CLASSES",
                "DEFAULT_RENDERER_CLASSES",
                "DEFAULT_PARSER_CLASSES",
            }
        )
    )

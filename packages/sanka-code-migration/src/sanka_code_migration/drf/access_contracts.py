# SPDX-License-Identifier: Apache-2.0
"""Bounded source contracts for membership access and request-scoped queries.

These functions inspect source and model metadata. They never call a customer's
permission, queryset or write method. Unrecognized statements reject the whole
contract; recognition never grants an unconditional fallback.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import textwrap
from typing import Any


class UnsupportedContract(ValueError):
    pass


def function_node(method: Any) -> ast.FunctionDef:
    module = ast.parse(textwrap.dedent(inspect.getsource(method)))
    if len(module.body) != 1 or not isinstance(module.body[0], ast.FunctionDef):
        raise UnsupportedContract()
    node = module.body[0]
    if (
        node.decorator_list
        or node.args.defaults
        or node.args.kw_defaults
        or node.args.vararg
        or node.args.posonlyargs
    ):
        raise UnsupportedContract()
    node.body = [
        s
        for s in node.body
        if not (
            isinstance(s, ast.Expr)
            and isinstance(s.value, ast.Constant)
            and isinstance(s.value.value, str)
        )
    ]
    return node


def chain(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [*chain(node.value), node.attr]
    raise UnsupportedContract()


def _field(model: Any, name: str) -> Any:
    try:
        return model._meta.pk if name == "pk" else model._meta.get_field(name)
    except Exception as error:
        raise UnsupportedContract() from error


def plain_model(model: Any) -> bool:
    models = importlib.import_module("django.db.models")
    candidate: Any = model
    return bool(
        inspect.isclass(model)
        and issubclass(model, models.Model)
        and not candidate._meta.proxy
        and not candidate._meta.parents
        and type(getattr(candidate, "objects", None)) is models.Manager
        and candidate.objects._queryset_class is models.QuerySet
        and candidate._default_manager is candidate.objects
        and candidate.__getattribute__ is models.Model.__getattribute__
        and all(f.column == f.attname for f in candidate._meta.concrete_fields)
    )


def model_identity(model: Any) -> dict[str, Any]:
    if not plain_model(model):
        raise UnsupportedContract()
    pk = model._meta.pk
    kinds = {"AutoField": "integer", "BigAutoField": "big_integer", "UUIDField": "uuid"}
    if pk.get_internal_type() not in kinds:
        raise UnsupportedContract()
    return {
        "db_table": str(model._meta.db_table),
        "pk": str(pk.column),
        "pk_kind": kinds[pk.get_internal_type()],
        "object_name": str(model._meta.object_name),
    }


def membership(model: Any, path: list[str]) -> dict[str, Any]:
    """Resolve one forward FK (optional) and an automatic M2M to AUTH_USER_MODEL."""
    if len(path) not in (1, 2):
        raise UnsupportedContract()
    parent = model
    foreign_key = None
    if len(path) == 2:
        fk = _field(model, path[0])
        if not fk.many_to_one or not fk.target_field.primary_key or fk.null:
            raise UnsupportedContract()
        parent = fk.related_model
        foreign_key = str(fk.attname)
    identity = model_identity(parent)
    relation = _field(parent, path[-1])
    auth = importlib.import_module("django.contrib.auth")
    user = auth.get_user_model()
    if not relation.many_to_many or relation.related_model is not user:
        raise UnsupportedContract()
    through = relation.remote_field.through
    if not through._meta.auto_created or relation.remote_field.symmetrical:
        raise UnsupportedContract()
    source = through._meta.get_field(relation.m2m_field_name())
    target = through._meta.get_field(relation.m2m_reverse_field_name())
    if not source.target_field.primary_key or not target.target_field.primary_key:
        raise UnsupportedContract()
    signals = importlib.import_module("django.db.models.signals")
    if signals.m2m_changed.has_listeners(through):
        raise UnsupportedContract()
    return {
        **identity,
        "foreign_key": foreign_key,
        "field": path[-1],
        "table": str(through._meta.db_table),
        "parent_column": str(source.column),
        "user_column": str(target.column),
    }


def user_identity() -> dict[str, str]:
    user = importlib.import_module("django.contrib.auth").get_user_model()
    models = importlib.import_module("django.db.models")
    manager = user._default_manager
    base_user = importlib.import_module("django.contrib.auth.base_user").AbstractBaseUser
    deferred = importlib.import_module("django.db.models.query_utils").DeferredAttribute
    if (
        user._meta.proxy
        or user._meta.parents
        or user.__getattribute__ is not models.Model.__getattribute__
        or user.__eq__ is not models.Model.__eq__
        or user.__init__ is not models.Model.__init__
        or user.from_db.__func__ is not models.Model.from_db.__func__
        or inspect.getattr_static(user, "is_authenticated")
        is not inspect.getattr_static(base_user, "is_authenticated")
        or any(
            type(inspect.getattr_static(user, name)) is not deferred
            for name in ("is_active", "is_superuser")
        )
        or manager.get_queryset.__func__ is not models.Manager.get_queryset
        or manager._queryset_class is not models.QuerySet
        or user._meta.ordering
    ):
        raise UnsupportedContract()
    active = _field(user, "is_active")
    superuser = _field(user, "is_superuser")
    if (
        active.get_internal_type() != "BooleanField"
        or superuser.get_internal_type() != "BooleanField"
    ):
        raise UnsupportedContract()
    if user._meta.pk.get_internal_type() not in ("AutoField", "BigAutoField"):
        raise UnsupportedContract()
    return {
        "table": str(user._meta.db_table),
        "pk": str(user._meta.pk.column),
        "active": str(active.column),
        "superuser": str(superuser.column),
    }


def _return_true(node: ast.If) -> bool:
    return (
        not node.orelse
        and len(node.body) == 1
        and isinstance(node.body[0], ast.Return)
        and isinstance(node.body[0].value, ast.Constant)
        and node.body[0].value.value is True
    )


def object_lookup_helper(value: Any) -> bool:
    return value in (
        importlib.import_module("django.shortcuts").get_object_or_404,
        importlib.import_module("rest_framework.generics").get_object_or_404,
    )


def capture_membership_permission(permission: Any, model: Any) -> dict[str, Any] | None:
    """Exact superuser-or-membership idiom, with object or URL-parent lookup."""
    try:
        base = importlib.import_module("rest_framework.permissions").BasePermission
        if not inspect.isclass(permission) or not issubclass(permission, base):
            return None
        permission_type: Any = permission
        object_method = permission_type.has_object_permission is not base.has_object_permission
        view_method = permission_type.has_permission is not base.has_permission
        if object_method == view_method:
            return None
        method = (
            permission_type.has_object_permission
            if object_method
            else permission_type.has_permission
        )
        for cls in permission.__mro__:
            if cls is base:
                break
            for name, value in vars(cls).items():
                if name in {"message", "code"} or (
                    name not in {"has_permission", "has_object_permission"}
                    and (
                        inspect.isroutine(value)
                        or isinstance(value, (property, classmethod, staticmethod))
                    )
                ):
                    return None
        func = function_node(method)
        names = [a.arg for a in func.args.args]
        if len(names) != (4 if object_method else 3) or func.args.kwarg or func.args.kwonlyargs:
            return None
        _, request, view, *objects = names
        body = list(func.body)
        if not (
            isinstance(body[0], ast.If)
            and _return_true(body[0])
            and chain(body[0].test) == [request, "user", "is_superuser"]
        ):
            return None
        body.pop(0)
        parent_param = None
        if view_method:
            assigned = body.pop(0)
            if not isinstance(assigned, ast.Assign) or len(assigned.targets) != 1:
                return None
            obj = chain(assigned.targets[0])[0]
            call = assigned.value
            if not isinstance(call, ast.Call) or len(call.args) != 1 or len(call.keywords) != 1:
                return None
            globals_ = method.__globals__
            if len(chain(call.func)) != 1 or len(chain(call.args[0])) != 1:
                return None
            helper = globals_.get(chain(call.func)[0])
            if not object_lookup_helper(helper):
                return None
            parent_model = globals_.get(chain(call.args[0])[0])
            keyword = call.keywords[0]
            if keyword.arg != "pk":
                return None
            value = keyword.value
            if not (
                isinstance(value, ast.Call)
                and chain(value.func) == [view, "kwargs", "get"]
                and len(value.args) == 1
                and not value.keywords
                and isinstance(value.args[0], ast.Constant)
                and isinstance(value.args[0].value, str)
            ):
                return None
            parent_param = value.args[0].value
            model = parent_model
        else:
            obj = objects[0]
        if len(body) != 2 or not isinstance(body[0], ast.If) or not _return_true(body[0]):
            return None
        condition = body[0].test
        if not (
            isinstance(condition, ast.Compare)
            and len(condition.ops) == 1
            and isinstance(condition.ops[0], ast.In)
            and chain(condition.left) == [request, "user"]
        ):
            return None
        members = condition.comparators[0]
        if not isinstance(members, ast.Call) or members.args or members.keywords:
            return None
        path = chain(members.func)
        if path[0] != obj or path[-1] != "all":
            return None
        if not (
            isinstance(body[1], ast.Return)
            and isinstance(body[1].value, ast.Constant)
            and body[1].value.value is False
        ):
            return None
        return {
            "kind": "membership",
            "relation": membership(model, path[1:-1]),
            "parent_param": parent_param,
            "superuser": True,
        }
    except (
        UnsupportedContract,
        AttributeError,
        IndexError,
        KeyError,
        TypeError,
        OSError,
        SyntaxError,
    ):
        return None


def capture_query(view: Any, model: Any) -> dict[str, Any] | None:
    """Filter/order chains using request.user, path kwargs, and membership subqueries."""
    try:
        method = view.get_queryset
        func = function_node(method)
        if len(func.args.args) != 1 or func.args.kwarg or func.args.kwonlyargs:
            return None
        self_name = func.args.args[0].arg
        values: dict[str, Any] = {}
        used: set[str] = set()
        globals_ = method.__globals__

        def evaluate(node: ast.expr) -> Any:
            if isinstance(node, ast.Name) and node.id in values:
                used.add(node.id)
                return values[node.id]
            if isinstance(node, ast.Constant) and type(node.value) in (str, bool, int):
                return {"kind": "constant", "value": node.value}
            if isinstance(node, ast.Attribute) and chain(node) == [self_name, "request", "user"]:
                return {"kind": "user"}
            if (
                isinstance(node, ast.Subscript)
                and chain(node.value) == [self_name, "kwargs"]
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                return {"kind": "kwarg", "name": node.slice.value}
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                raise UnsupportedContract()
            if isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "objects":
                target_path = chain(node.func.value.value)
                if len(target_path) != 1:
                    raise UnsupportedContract()
                target = globals_.get(target_path[0])
                model_identity(target)
                prior = {"kind": "query", "model": target, "filters": [], "ordering": []}
            else:
                prior = evaluate(node.func.value)
            if prior.get("kind") != "query":
                raise UnsupportedContract()
            prior = {**prior, "filters": list(prior["filters"])}
            if node.func.attr == "order_by" and node.args and not node.keywords:
                ordering = [ast.literal_eval(arg) for arg in node.args]
                if not all(isinstance(x, str) for x in ordering):
                    raise UnsupportedContract()
                for term in ordering:
                    field = _field(prior["model"], term.lstrip("-"))
                    if not field.concrete or field.is_relation:
                        raise UnsupportedContract()
                prior["ordering"] = ordering
            elif node.func.attr == "filter" and not node.args and node.keywords:
                for key in node.keywords:
                    if key.arg is None:
                        raise UnsupportedContract()
                    value = evaluate(key.value)
                    if value["kind"] == "user":
                        prior["filters"].append(
                            {"kind": "member", "relation": membership(prior["model"], [key.arg])}
                        )
                    elif value["kind"] == "query" and key.arg.endswith("__in"):
                        fk = _field(prior["model"], key.arg[:-4])
                        if (
                            not fk.many_to_one
                            or fk.related_model is not value["model"]
                            or not fk.target_field.primary_key
                            or len(value["filters"]) != 1
                            or value["filters"][0]["kind"] != "member"
                        ):
                            raise UnsupportedContract()
                        relation = dict(value["filters"][0]["relation"])
                        relation["foreign_key"] = str(fk.attname)
                        prior["filters"].append({"kind": "member", "relation": relation})
                    elif value["kind"] == "kwarg":
                        field = _field(prior["model"], key.arg)
                        if not field.concrete or field.many_to_many:
                            raise UnsupportedContract()
                        if field.is_relation and not field.target_field.primary_key:
                            raise UnsupportedContract()
                        prior["filters"].append(
                            {"kind": "equal", "field": str(field.attname), "value": value}
                        )
                    else:
                        raise UnsupportedContract()
            elif node.func.attr != "all" or node.args or node.keywords:
                raise UnsupportedContract()
            return prior

        for statement in func.body[:-1]:
            if not (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
            ):
                return None
            name = statement.targets[0].id
            if name == self_name or name in values or name in globals_:
                return None
            values[name] = evaluate(statement.value)
        last = func.body[-1]
        if not isinstance(last, ast.Return) or last.value is None:
            return None
        result = evaluate(last.value)
        if result.get("kind") != "query" or result["model"] is not model or set(values) != used:
            return None
        return {"version": 1, "filters": result["filters"], "ordering": result["ordering"]}
    except (
        UnsupportedContract,
        AttributeError,
        IndexError,
        KeyError,
        TypeError,
        ValueError,
        OSError,
        SyntaxError,
    ):
        return None


def capture_creator_membership(view: Any, model: Any) -> dict[str, Any] | None:
    try:
        func = function_node(view.perform_create)
        self_name, serializer = (a.arg for a in func.args.args)
        saved = func.body[0]
        if not isinstance(saved, ast.Assign) or len(saved.targets) != 1:
            return None
        instance = chain(saved.targets[0])[0]
        path = chain(func.body[1].value.func)  # type: ignore[attr-defined]
        field = path[1]
        expected = ast.parse(
            f"def perform_create({self_name}, {serializer}):\n"
            f"    {instance} = {serializer}.save()\n"
            f"    {instance}.{field}.add({self_name}.request.user)\n"
            f"    return {instance}\n"
        ).body[0]
        assert isinstance(expected, ast.FunctionDef)
        # Annotations do not execute inside the body; every executable statement must match.
        if ast.dump(ast.Module(body=func.body, type_ignores=[])) != ast.dump(
            ast.Module(body=expected.body, type_ignores=[])
        ):
            return None
        if len({self_name, serializer, instance}) != 3 or func.args.kwarg or func.args.kwonlyargs:
            return None
        writes = importlib.import_module("sanka_code_migration.drf.nested_create")
        if not writes.supports_default_model_writes(model):
            return None
        return membership(model, [field])
    except (
        UnsupportedContract,
        AttributeError,
        IndexError,
        KeyError,
        TypeError,
        ValueError,
        OSError,
        SyntaxError,
    ):
        return None


def capture_parent_create(serializer: Any, model: Any) -> dict[str, Any] | None:
    """URL parent assignment and an exact duplicate predicate before default create."""
    try:
        func = function_node(serializer.create)
        self_name, data = (a.arg for a in func.args.args)
        if func.args.kwonlyargs:
            return None
        assigned, checked, _returned = func.body
        if not isinstance(assigned, ast.Assign) or len(assigned.targets) != 1:
            return None
        target = assigned.targets[0]
        if not isinstance(target, ast.Subscript) or chain(target.value) != [data]:
            return None
        column = ast.literal_eval(target.slice)
        field = _field(model, column)
        if not field.many_to_one or not field.target_field.primary_key:
            return None
        parent = field.related_model
        identity = model_identity(parent)
        if not isinstance(assigned.value, ast.Subscript):
            return None
        param = ast.literal_eval(assigned.value.slice)
        if not isinstance(param, str) or not isinstance(checked, ast.If):
            return None
        call = checked.test
        if not isinstance(call, ast.Call) or len(call.keywords) != 2:
            return None
        duplicate_name, duplicate_flag = (key.arg for key in call.keywords)
        if duplicate_name is None or duplicate_flag is None:
            return None
        name_field = _field(model, duplicate_name)
        flag_field = _field(model, duplicate_flag)
        if (
            name_field.get_internal_type() != "CharField"
            or flag_field.get_internal_type() != "BooleanField"
        ):
            return None
        reverse = field.remote_field.get_accessor_name()
        parent_alias = next(
            (key for key, value in serializer.create.__globals__.items() if value is parent), None
        )
        if parent_alias is None:
            return None
        error = checked.body[0]
        if not isinstance(error, ast.Raise) or not isinstance(error.exc, ast.Call):
            return None
        error_path = chain(error.exc.func)
        module = serializer.create.__globals__.get(error_path[0])
        drf = importlib.import_module("rest_framework.serializers")
        if len(error_path) != 2 or module is not drf or error_path[1] != "ValidationError":
            return None
        message = ast.literal_eval(error.exc.args[0])
        if not isinstance(message, str):
            return None
        context = f'{self_name}.context["request"].parser_context["kwargs"][{param!r}]'
        expected = ast.parse(
            f"{data}[{column!r}] = {context}\n"
            f"if {parent_alias}.objects.get(id={context}).{reverse}.filter("
            f"{duplicate_name}={data}[{duplicate_name!r}], {duplicate_flag}=False):\n"
            f"    raise {error_path[0]}.ValidationError({message!r})\n"
            f"return super().create({data})\n"
        )
        if parent._meta.pk.name != "id" or ast.dump(
            ast.Module(body=func.body, type_ignores=[])
        ) != ast.dump(expected):
            return None
        writes = importlib.import_module("sanka_code_migration.drf.nested_create")
        if not writes.supports_default_model_writes(model, parent):
            return None
        return {
            "style": "parent_duplicate",
            "parent": identity,
            "param": param,
            "column": str(field.attname),
            "name": str(name_field.attname),
            "flag": str(flag_field.attname),
            "message": message,
        }
    except (
        UnsupportedContract,
        AttributeError,
        IndexError,
        KeyError,
        TypeError,
        ValueError,
        OSError,
        SyntaxError,
    ):
        return None


def capture_member_update(serializer: Any, model: Any) -> dict[str, Any] | None:
    """Match add/remove loops, including the original save after every member."""
    try:
        func = function_node(serializer.update)
        self_name, instance, data = (a.arg for a in func.args.args)
        loop, _returned = func.body
        if not isinstance(loop, ast.For):
            return None
        member = chain(loop.target)[0]
        method = chain(loop.body[0].value.func)  # type: ignore[attr-defined]
        if len(method) != 3 or method[0] != instance or method[2] not in ("add", "remove"):
            return None
        field, action = method[1:]
        expected = ast.parse(
            f"for {member} in {data}[{field!r}]:\n"
            f"    {instance}.{field}.{action}({member})\n"
            f"    {instance}.save()\n"
            f"return {instance}\n"
        )
        if ast.dump(ast.Module(body=func.body, type_ignores=[])) != ast.dump(expected):
            return None
        if func.args.kwarg or func.args.kwonlyargs or len({self_name, instance, data, member}) != 4:
            return None
        writes = importlib.import_module("sanka_code_migration.drf.nested_create")
        if not writes.supports_default_model_writes(model):
            return None
        drf = importlib.import_module("rest_framework.serializers")
        for cls in serializer.__mro__:
            if cls is drf.ModelSerializer:
                break
            for name, value in vars(cls).items():
                if name != "update" and (
                    inspect.isroutine(value)
                    or isinstance(value, (property, classmethod, staticmethod))
                ):
                    return None
        instance_serializer = serializer()
        if instance_serializer.validators:
            return None
        fields = instance_serializer.fields
        relations = importlib.import_module("rest_framework.relations")
        if set(fields) != {field} or type(fields[field]) is not relations.ManyRelatedField:
            return None
        outer = fields[field]
        inner = outer.child_relation
        drf = importlib.import_module("rest_framework.serializers")
        if (
            type(inner) is not relations.PrimaryKeyRelatedField
            or inner.pk_field is not None
            or inner.allow_null
            or outer.read_only
            or outer.allow_null
            or outer.default is not drf.empty
            or outer.write_only
            or outer.validators
            or inner.validators
            or outer.source != field
            or serializer.create is not drf.ModelSerializer.create
            or serializer.validate is not drf.Serializer.validate
            or serializer.to_representation is not drf.ModelSerializer.to_representation
            or any(name.startswith("validate_") for name in vars(serializer))
        ):
            return None
        relation = membership(model, [field])
        auth = importlib.import_module("django.contrib.auth")
        models = importlib.import_module("django.db.models")
        query = inner.queryset
        if isinstance(query, models.Manager):
            if query.get_queryset.__func__ is not models.Manager.get_queryset:
                return None
            query = query.all()
        if (
            type(query) is not models.QuerySet
            or query.model is not auth.get_user_model()
            or query.query.where.children
            or query.query.is_sliced
            or query.query.combinator
            or query.query.annotations
            or query.query.distinct
            or query.query.values_select
        ):
            return None
        touches = []
        for column in model._meta.concrete_fields:
            if getattr(column, "auto_now", False):
                if (
                    column.get_internal_type() != "DateTimeField"
                    or not importlib.import_module("django.conf").settings.USE_TZ
                ):
                    return None
                touches.append(str(column.column))
        return {
            "action": action,
            "relation": relation,
            "field": field,
            "touches": touches,
            "required": bool(outer.required),
            "allow_empty": bool(outer.allow_empty),
            "messages": {str(k): str(v) for k, v in outer.error_messages.items()},
            "child_messages": {str(k): str(v) for k, v in inner.error_messages.items()},
            "user": user_identity(),
        }
    except (
        UnsupportedContract,
        AttributeError,
        IndexError,
        KeyError,
        TypeError,
        ValueError,
        OSError,
        SyntaxError,
    ):
        return None


def capture_member_action(view: Any) -> tuple[Any, Any, dict[str, Any]] | None:
    """Exact APIView PUT: get parent, check permission, validate, save, respond."""
    try:
        base = importlib.import_module("rest_framework.views").APIView
        if not issubclass(view, base) or not callable(getattr(view, "put", None)):
            return None
        # Only source PUT and declarative attributes may differ from APIView.
        for cls in view.__mro__:
            if cls is base:
                break
            for name, value in vars(cls).items():
                if name != "put" and (
                    inspect.isroutine(value)
                    or isinstance(value, (property, classmethod, staticmethod))
                ):
                    return None
        if any(
            getattr(view, name) != getattr(base, name)
            for name in (
                "parser_classes",
                "renderer_classes",
                "content_negotiation_class",
                "metadata_class",
            )
        ):
            return None
        method = inspect.unwrap(view.put)
        # Schema decoration is introspection-only; unknown wrappers remain unsupported.
        if method is not view.put:
            return None
        module = ast.parse(textwrap.dedent(inspect.getsource(method)))
        func = module.body[0]
        if not isinstance(func, ast.FunctionDef):
            return None
        for decorator in func.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Name):
                return None
            dec = method.__globals__.get(decorator.func.id)
            if (
                getattr(dec, "__module__", None) != "drf_spectacular.utils"
                or getattr(dec, "__name__", None) != "extend_schema"
            ):
                return None
        self_name, request, pk, _format_name = (arg.arg for arg in func.args.args)
        if (
            func.args.kwarg
            or func.args.vararg
            or func.args.kwonlyargs
            or len(func.args.defaults) != 1
            or ast.literal_eval(func.args.defaults[0]) is not None
        ):
            return None
        parent_assign, serializer_assign, _, _, _ = func.body
        parent_name = chain(parent_assign.targets[0])[0]  # type: ignore[attr-defined]
        serializer_name = chain(serializer_assign.targets[0])[0]  # type: ignore[attr-defined]
        parent_call = parent_assign.value  # type: ignore[attr-defined]
        helper = chain(parent_call.func)[0]
        parent_alias = chain(parent_call.args[0])[0]
        serializer_alias = chain(serializer_assign.value.func)[0]  # type: ignore[attr-defined]
        globals_ = method.__globals__
        if not object_lookup_helper(globals_.get(helper)):
            return None
        model = globals_.get(parent_alias)
        serializer = globals_.get(serializer_alias)
        contract = capture_member_update(serializer, model)
        if contract is None:
            return None
        response_aliases = [
            n
            for n, v in globals_.items()
            if v is importlib.import_module("rest_framework.response").Response
        ]
        status_aliases = [
            n for n, v in globals_.items() if v is importlib.import_module("rest_framework.status")
        ]
        for response in response_aliases:
            for status in status_aliases:
                expected = ast.parse(
                    f"{parent_name} = {helper}({parent_alias}, pk={pk})\n"
                    f"{serializer_name} = {serializer_alias}({parent_name}, data={request}.data)\n"
                    f"{self_name}.check_object_permissions({request}, {parent_name})\n"
                    f"if {serializer_name}.is_valid():\n"
                    f"    {serializer_name}.save()\n"
                    f"    return {response}({serializer_name}.data)\n"
                    f"return {response}({serializer_name}.errors, "
                    f"status={status}.HTTP_400_BAD_REQUEST)\n"
                )
                # Source has five statements, including the final error response.
                if ast.dump(ast.Module(body=func.body, type_ignores=[])) == ast.dump(expected):
                    contract["param"] = pk
                    return model, serializer, contract
        return None
    except (
        UnsupportedContract,
        AttributeError,
        IndexError,
        KeyError,
        TypeError,
        ValueError,
        OSError,
        SyntaxError,
    ):
        return None


def capture_computed_preview(field: Any, model: Any) -> dict[str, Any] | None:
    """A read-only projection of the first N related rows with one Boolean filter."""
    try:
        method = getattr(field.parent, field.method_name)
        func = function_node(method)
        _self_name, obj = (arg.arg for arg in func.args.args)
        returned = func.body[0]
        if len(func.body) != 1 or not isinstance(returned, ast.Return):
            return None
        sliced = returned.value
        if not isinstance(sliced, ast.Subscript) or not isinstance(sliced.slice, ast.Slice):
            return None
        if sliced.slice.upper is None:
            return None
        limit = ast.literal_eval(sliced.slice.upper)
        if type(limit) is not int or not 0 < limit <= 100:
            return None
        comp = sliced.value
        if not isinstance(comp, ast.ListComp) or len(comp.generators) != 1:
            return None
        generator = comp.generators[0]
        item = chain(generator.target)[0]
        call = generator.iter
        if not isinstance(call, ast.Call) or len(call.keywords) != 1:
            return None
        path = chain(call.func)
        if len(path) != 3 or path[0] != obj or path[-1] != "filter":
            return None
        related_name = path[1]
        relation = _field(model, related_name)
        if not relation.one_to_many or not relation.field.target_field.primary_key:
            return None
        related_model = relation.related_model
        identity = model_identity(related_model)
        key = call.keywords[0].arg
        if key is None:
            return None
        value = ast.literal_eval(call.keywords[0].value)
        if (
            type(value) is not bool
            or _field(related_model, key).get_internal_type() != "BooleanField"
        ):
            return None
        if not isinstance(comp.elt, ast.Dict) or len(comp.elt.keys) != 1:
            return None
        if comp.elt.keys[0] is None:
            return None
        output = ast.literal_eval(comp.elt.keys[0])
        attribute = chain(comp.elt.values[0])
        if len(attribute) != 2 or attribute[0] != item or not isinstance(output, str):
            return None
        if key is None:
            return None
        column = _field(related_model, attribute[1])
        if column.get_internal_type() != "CharField":
            return None
        expected = ast.parse(
            f"return [{{{output!r}: {item}.{attribute[1]}}} for {item} "
            f"in {obj}.{related_name}.filter({key}={value!r})][:{limit}]"
        )
        if ast.dump(ast.Module(body=func.body, type_ignores=[])) != ast.dump(expected):
            return None
        ordering = []
        for term in related_model._meta.ordering or ():
            column_field = _field(related_model, term.lstrip("-"))
            if not column_field.concrete or column_field.is_relation:
                return None
            ordering.append(("-" if term.startswith("-") else "") + str(column_field.column))
        return {
            **identity,
            "foreign_key": str(relation.field.column),
            "column": str(column.column),
            "filter_column": str(_field(related_model, key).column),
            "value": value,
            "limit": limit,
            "output": output,
            "ordering": ordering,
        }
    except (
        UnsupportedContract,
        AttributeError,
        IndexError,
        KeyError,
        TypeError,
        ValueError,
        OSError,
        SyntaxError,
    ):
        return None


def capture_delete(model: Any) -> list[dict[str, str]] | None:
    """Direct stock CASCADE children and automatic member tables, atomically deleted."""
    try:
        models = importlib.import_module("django.db.models")
        signals = importlib.import_module("django.db.models.signals")
        writes = importlib.import_module("sanka_code_migration.drf.nested_create")
        children = []
        involved = [model]
        for related in model._meta.related_objects:
            field = related.field
            child = related.related_model
            if (
                not related.one_to_many
                or field.remote_field.on_delete is not models.CASCADE
                or not field.target_field.primary_key
                or child._meta.related_objects
                or child._meta.many_to_many
            ):
                return None
            involved.append(child)
            children.append({"table": str(child._meta.db_table), "column": str(field.column)})
        for relation in model._meta.many_to_many:
            member = membership(model, [relation.name])
            children.append({"table": member["table"], "column": member["parent_column"]})
        if not writes.supports_default_model_writes(*involved):
            return None
        for item in involved:
            if item.delete is not models.Model.delete or any(
                signal.has_listeners(item) for signal in (signals.pre_delete, signals.post_delete)
            ):
                return None
        return [
            *children,
            {"table": str(model._meta.db_table), "column": str(model._meta.pk.column)},
        ]
    except (UnsupportedContract, AttributeError, TypeError, ValueError):
        return None

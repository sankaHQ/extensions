# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""Fail-closed capture of bounded nested DRF replacement writes."""

from __future__ import annotations

import ast
from typing import Any


def composite_groups(value: Any) -> list[list[str]]:
    if not isinstance(value, (tuple, list, set)) or not value:
        raise ValueError("unique_together must contain literal field groups")
    groups = []
    for group in value:
        if (
            not isinstance(group, (tuple, list))
            or len(group) < 2
            or not all(isinstance(n, str) for n in group)
            or len(set(group)) != len(group)
        ):
            raise ValueError("invalid unique_together group")
        groups.append(list(group))
    if len({tuple(g) for g in groups}) != len(groups):
        raise ValueError("duplicate unique_together group")
    return sorted(groups)


def required_fields(
    method: ast.FunctionDef | None, names: list[str] | tuple[str, ...]
) -> list[str]:
    from .drf_project import _same

    if method is None:
        return []
    try:
        guard = method.body[0]
        assignment = guard.body[0]  # type: ignore[attr-defined]
        errors = assignment.targets[0].id
        generator = assignment.value.generators[0]
        field = generator.target.id
        required = ast.literal_eval(generator.iter)
        argument = method.args.args[1].arg
    except (AttributeError, IndexError, TypeError, ValueError) as exc:
        raise ValueError("unrecognized required-field hook") from exc
    if len({argument, errors, field}) != 3 or {argument, errors, field} & {
        "self",
        "isinstance",
        "dict",
        "super",
        "serializers",
    }:
        raise ValueError("required-field hook shadows captured bindings")
    expected = f"""def to_internal_value(self, {argument}):
    if isinstance({argument}, dict):
        {errors} = {{{field}: ["This field is required."] for {field} in {required!r} if {field} not in {argument}}}
        if {errors}:
            raise serializers.ValidationError({errors})
    return super().to_internal_value({argument})
"""
    if (
        not isinstance(required, (tuple, list))
        or not required
        or not all(isinstance(n, str) and n in names and n != "id" for n in required)
        or len(set(required)) != len(required)
        or not _same(method, expected)
    ):
        raise ValueError("unqualified required-field hook")
    return list(required)


def replacement_recipe(
    methods: dict[str, ast.FunctionDef],
    helper: str,
    model: dict[str, Any],
    nested: dict[str, Any],
    models: dict[str, dict[str, Any]],
) -> None:
    from .drf_project import _same

    child = nested["serializer"]
    grand = child.get("nested")
    if (
        not grand
        or grand["serializer"].get("nested")
        or set(methods) != {"create", "update", helper}
    ):
        raise ValueError("replacement recipe requires two nested collection levels")
    # Both errors must come from a captured parent-scoped composite constraint.
    for relation in (nested, grand):
        target = models[relation["serializer"]["model"]]
        constraints = target.get("constraints", [])
        foreign = relation["foreign_key"]
        if (
            len(constraints) != 1
            or len(constraints[0]["columns"]) != 2
            or constraints[0]["columns"][0] != foreign
            or any(f["unique"] for f in target["fields"] if not f["primary_key"])
        ):
            raise ValueError("nested replacement requires one parent-scoped unique pair")
        schema = relation["serializer"]
        required = {f["name"] for f in schema["fields"] if f["required"] and not f["read_only"]}
        if schema.get("nested"):
            required.add(schema["nested"]["name"])
        if not required <= set(schema.get("always_required", [])):
            raise ValueError("replacement children require explicit required-field hooks on PATCH")
        selected = {f["name"] for f in schema["fields"]} | {foreign}
        if any(f.get("required", True) and f["name"] not in selected for f in target["fields"]):
            raise ValueError("replacement child model requires uncaptured fields")
        key = constraints[0]["columns"][1]
        if not any(
            f["name"] == key and f["required"] and not f["read_only"]
            for f in relation["serializer"]["fields"]
        ):
            raise ValueError("nested unique key must be a required writable field")
    try:
        create: Any = methods["create"]
        update: Any = methods["update"]
        writer: Any = methods[helper]
        data = create.body[0].targets[0].id
        parent = create.body[1].body[0].targets[0].id
        update_data = update.body[0].targets[0].id
        name, value = [n.id for n in update.body[1].body[0].target.elts]
        arg_parent, arg_data = [a.arg for a in writer.args.args[1:]]
        loop = writer.body[0]
        index, item = [n.id for n in loop.target.elts]
        leaves = loop.body[0].targets[0].id
        attempt = loop.body[1]
        saved = attempt.body[0].targets[0].id
        handler = attempt.handlers[0]
        errors = handler.body[0].targets[0].id
        message = ast.literal_eval(handler.body[0].value.values[0].values[0])
        inner = loop.body[2]
        leaf_index, leaf = [n.id for n in inner.target.elts]
        leaf_handler = inner.body[0].handlers[0]
        leaf_errors = leaf_handler.body[0].targets[0].id
        leaf_message = ast.literal_eval(
            leaf_handler.body[0].value.values[0].values[0].values[0].values[0]
        )
    except (AttributeError, IndexError, TypeError, ValueError) as exc:
        raise ValueError("unrecognized nested replacement recipe") from exc
    reserved = {
        "self",
        "validated_data",
        "instance",
        "serializers",
        "transaction",
        "IntegrityError",
        "str",
        "enumerate",
        "setattr",
    } | set(models)
    scopes = [
        (data, parent),
        (update_data, name, value),
        (arg_parent, arg_data, index, item, leaves, saved, leaf_index, leaf),
    ]
    for bindings in scopes:
        if len(set(bindings)) != len(bindings) or set(bindings) & reserved:
            raise ValueError("nested write locals shadow captured bindings")
    error_names = {errors, leaf_errors}
    exception_names = {handler.name, leaf_handler.name}
    if (
        None in exception_names
        or error_names & exception_names
        or (error_names | exception_names) & (set(scopes[2]) | reserved)
    ):
        raise ValueError("nested error handlers shadow write bindings")
    for msg in (message, leaf_message):
        if not isinstance(msg, list) or len(msg) != 1 or type(msg[0]) is not str:
            raise ValueError("nested integrity errors require one literal message")
    child_fk = next(
        f["source_name"]
        for f in models[child["model"]]["fields"]
        if f["name"] == nested["foreign_key"]
    )
    leaf_fk = next(
        f["source_name"]
        for f in models[grand["serializer"]["model"]]["fields"]
        if f["name"] == grand["foreign_key"]
    )
    expected_create = f"""def create(self, validated_data):
    {data} = validated_data.pop({nested["name"]!r})
    with transaction.atomic():
        {parent} = {model["name"]}.objects.create(**validated_data)
        self.{helper}({parent}, {data})
    return {parent}
"""
    expected_update = f"""def update(self, instance, validated_data):
    {update_data} = validated_data.pop({nested["name"]!r}, serializers.empty)
    with transaction.atomic():
        for {name}, {value} in validated_data.items():
            setattr(instance, {name}, {value})
        instance.save()
        if {update_data} is not serializers.empty:
            instance.{nested["name"]}.all().delete()
            self.{helper}(instance, {update_data})
    return instance
"""
    expected_helper = f"""def {helper}(self, {arg_parent}, {arg_data}):
    for {index}, {item} in enumerate({arg_data}):
        {leaves} = {item}.pop({grand["name"]!r})
        try:
            {saved} = {child["model"]}.objects.create({child_fk}={arg_parent}, **{item})
        except IntegrityError as {handler.name}:
            {errors} = {{str({index}): {{"non_field_errors": {message!r}}}}}
            raise serializers.ValidationError({{{nested["name"]!r}: {errors}}}) from {handler.name}
        for {leaf_index}, {leaf} in enumerate({leaves}):
            try:
                {grand["serializer"]["model"]}.objects.create({leaf_fk}={saved}, **{leaf})
            except IntegrityError as {leaf_handler.name}:
                {leaf_errors} = {{str({index}): {{{grand["name"]!r}: {{str({leaf_index}): {{"non_field_errors": {leaf_message!r}}}}}}}}}
                raise serializers.ValidationError({{{nested["name"]!r}: {leaf_errors}}}) from {leaf_handler.name}
"""
    if not all(
        _same(methods[n], src)
        for n, src in (
            ("create", expected_create),
            ("update", expected_update),
            (helper, expected_helper),
        )
    ):
        raise ValueError("nested replacement hooks contain uncaptured behavior")
    nested["integrity_error"] = message
    grand["integrity_error"] = leaf_message


def serializer_annotations(tree: ast.Module) -> None:
    """Only deferred, inert method annotations may be discarded for AST matching."""
    if not tree.body or ast.unparse(tree.body[0]) != "from __future__ import annotations":
        return
    tree.body.pop(0)
    allowed = {"Any", "dict", "list", "str", "int", "bool"}
    allowed.update(n.name for n in tree.body if isinstance(n, ast.ClassDef))
    for n in tree.body:
        if isinstance(n, ast.ImportFrom) and (n.module or "").endswith(".models"):
            allowed.update(a.asname or a.name for a in n.names)
    for node in ast.walk(tree):
        if isinstance(node, (ast.arg, ast.FunctionDef)):
            attr = "annotation" if isinstance(node, ast.arg) else "returns"
            annotation = getattr(node, attr)
            if annotation is None:
                continue
            for item in ast.walk(annotation):
                if (
                    not isinstance(
                        item, (ast.Name, ast.Subscript, ast.Tuple, ast.Load, ast.Constant)
                    )
                    or (isinstance(item, ast.Name) and item.id not in allowed)
                    or (isinstance(item, ast.Constant) and item.value is not None)
                ):
                    raise ValueError("unqualified serializer method annotation")
            setattr(node, attr, None)
    for node in list(tree.body):
        if isinstance(node, ast.ImportFrom) and ast.unparse(node) == "from typing import Any":
            tree.body.remove(node)
    if any(isinstance(n, ast.Name) and n.id == "Any" for n in ast.walk(tree)):
        raise ValueError("typing.Any is only qualified in deferred annotations")


def prefetch_queryset(attrs: dict[str, ast.expr], schema: dict[str, Any]) -> dict[str, ast.expr]:
    """Stock literal prefetches change loading only; never discard custom querysets."""
    node = attrs.get("queryset")
    if not (
        isinstance(node, ast.Call)
        and not node.args
        and not node.keywords
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "all"
        and isinstance(node.func.value, ast.Call)
    ):
        return attrs
    prefetch = node.func.value
    if (
        not isinstance(prefetch.func, ast.Attribute)
        or ast.unparse(prefetch.func) != schema["model"] + ".objects.prefetch_related"
        or not prefetch.args
        or prefetch.keywords
    ):
        return attrs
    paths = set()
    path = []
    current = schema
    while current.get("nested"):
        path.append(current["nested"]["name"])
        paths.add("__".join(path))
        current = current["nested"]["serializer"]
    if any(
        not isinstance(arg, ast.Constant) or type(arg.value) is not str or arg.value not in paths
        for arg in prefetch.args
    ):
        raise ValueError("prefetch requires a captured serializer relationship path")
    return attrs | {"queryset": ast.parse(schema["model"] + ".objects.all()", mode="eval").body}

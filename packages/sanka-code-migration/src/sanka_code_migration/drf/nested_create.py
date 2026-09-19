# SPDX-License-Identifier: Apache-2.0
"""Lower a complete, bounded nested-create idiom; never discard source statements."""

from __future__ import annotations

import ast
import importlib
from typing import Any


def supports_default_model_writes(*model_classes: Any) -> bool:
    """Reject Django write behavior that a generated Tortoise store cannot preserve."""
    models = importlib.import_module("django.db.models")
    signals = importlib.import_module("django.db.models.signals")
    router = importlib.import_module("django.db").router
    if router.routers:
        return False
    for model in model_classes:
        if model is None or model._meta.proxy or model._meta.parents:
            return False
        if any(
            getattr(model, name) is not getattr(models.Model, name)
            for name in ("__init__", "save", "save_base")
        ):
            return False
        for manager in (
            getattr(model, "objects", None),
            model._default_manager,
            model._base_manager,
        ):
            if (
                manager is None
                or type(manager) is not models.Manager
                or manager._queryset_class is not models.QuerySet
            ):
                return False
        if any(
            getattr(signals, name).has_listeners(model)
            for name in ("pre_init", "post_init", "pre_save", "post_save")
        ):
            return False
        if any(
            not type(field).__module__.startswith("django.db.models.fields")
            for field in model._meta.concrete_fields
        ):
            return False
    return True


def lower_nested_create(
    source: str,
    *,
    parent_aliases: set[str],
    child_aliases: set[str],
    transaction_aliases: set[str],
    error_aliases: set[str],
    field: str,
    foreign_key: str,
    integer_fields: set[str],
) -> dict[str, Any] | None:
    """Match the entire function against its reconstructed declarative equivalent.

    One parent, one child collection, one transaction, and optionally one integer
    sum guard are supported. Names and constants are source-derived. Anything
    additional (including calls, defaults, decorators, filters, or side effects)
    fails the structural comparison.
    """
    try:
        module = ast.parse(source)
        function = module.body[0]
        if not (isinstance(function, ast.FunctionDef) and len(module.body) == 1):
            raise ValueError("unsupported nested create")
        (self_name, data) = (arg.arg for arg in function.args.args)
        (pop, atomic, returned) = function.body
        items = pop.targets[0].id  # type: ignore[attr-defined]
        if not isinstance(atomic, ast.With):
            raise ValueError("unsupported nested create")
        transaction = atomic.items[0].context_expr.func.value.id  # type: ignore[attr-defined]
        if transaction not in transaction_aliases:
            raise ValueError("unsupported nested create")
        (parent_create, children_create, *guard) = atomic.body
        parent = parent_create.targets[0].id  # type: ignore[attr-defined]
        parent_model = parent_create.value.func.value.value.id  # type: ignore[attr-defined]
        if parent_model not in parent_aliases:
            raise ValueError("unsupported nested create")
        child = children_create.target.id  # type: ignore[attr-defined]
        child_model = children_create.body[0].value.func.value.value.id  # type: ignore[attr-defined]
        if child_model not in child_aliases:
            raise ValueError("unsupported nested create")
        if not isinstance(returned, ast.Return):
            raise ValueError("unsupported nested create")
        names = [self_name, data, items, parent, child]
        if not len(set(names)) == len(names):
            raise ValueError("unsupported nested create")
        if set(names) & (parent_aliases | child_aliases | transaction_aliases | {"sum"}):
            raise ValueError("unsupported nested create")
        rule: dict[str, Any] | None = None
        guard_source = ""
        if guard:
            (summed, checked) = guard
            total = summed.targets[0].id  # type: ignore[attr-defined]
            expression = summed.value.args[0]  # type: ignore[attr-defined]
            if not isinstance(expression, ast.GeneratorExp):
                raise ValueError("unsupported nested create")
            quantity = expression.elt.attr  # type: ignore[attr-defined]
            item = expression.generators[0].target.id  # type: ignore[attr-defined]
            if quantity not in integer_fields:
                raise ValueError("unsupported nested create")
            if not (isinstance(checked, ast.If) and isinstance(checked.test, ast.Compare)):
                raise ValueError("unsupported nested create")
            limit = ast.literal_eval(checked.test.comparators[0])
            if type(limit) is not int:
                raise ValueError("unsupported nested create")
            operation = type(checked.test.ops[0]).__name__
            symbol = {"Gt": ">", "GtE": ">=", "Lt": "<", "LtE": "<=", "Eq": "==", "NotEq": "!="}[
                operation
            ]
            raised = checked.body[0]
            if not (isinstance(raised, ast.Raise) and isinstance(raised.exc, ast.Call)):
                raise ValueError("unsupported nested create")
            error = ast.unparse(raised.exc.func)
            if error not in error_aliases:
                raise ValueError("unsupported nested create")
            detail = ast.literal_eval(raised.exc.args[0])
            if not (isinstance(detail, dict) and detail):
                raise ValueError("unsupported nested create")
            if not all(
                isinstance(key, str)
                and isinstance(value, list)
                and value
                and all(isinstance(message, str) for message in value)
                for (key, value) in detail.items()
            ):
                raise ValueError("unsupported nested create")
            if not len({*names, total, item}) == len(names) + 2:
                raise ValueError("unsupported nested create")
            if {total, item} & (parent_aliases | child_aliases | transaction_aliases | {"sum"}):
                raise ValueError("unsupported nested create")
            rule = {
                "field": field,
                "attribute": quantity,
                "operator": operation,
                "limit": limit,
                "detail": detail,
            }
            guard_source = (
                f"        {total} = sum({item}.{quantity} for {item} in {parent}.{field}.all())\n"
                f"        if {total} {symbol} {limit!r}:\n"
                f"            raise {error}({detail!r})\n"
            )
        expected = (
            f"def {function.name}({self_name}, {data}):\n"
            f"    {items} = {data}.pop({field!r})\n"
            f"    with {transaction}.atomic():\n"
            f"        {parent} = {parent_model}.objects.create(**{data})\n"
            f"        for {child} in {items}:\n"
            f"            {child_model}.objects.create({foreign_key}={parent}, **{child})\n"
            f"{guard_source}"
            f"    return {parent}\n"
        )
        if ast.dump(module) != ast.dump(ast.parse(expected)):
            return None
        return {"style": "nested", "atomic": True, "rule": rule}
    except (
        AttributeError,
        IndexError,
        KeyError,
        TypeError,
        ValueError,
        SyntaxError,
    ):
        return None

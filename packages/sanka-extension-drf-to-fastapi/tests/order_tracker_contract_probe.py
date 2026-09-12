# SPDX-License-Identifier: Apache-2.0
"""Direct source/generated-handler contract test; no scan/plan/apply lifecycle."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch


def main() -> None:
    project = Path(sys.argv[1])
    generated = project / "generated"
    generated.mkdir()
    source_db = project / "source.sqlite3"
    target_db = project / "target.sqlite3"
    os.environ["DJANGO_SETTINGS_MODULE"] = "shop_config.settings"
    os.environ["BENCH_DB_PATH"] = str(source_db)
    os.environ["SANKA_DATABASE_URL"] = str(target_db)
    sys.path.insert(0, str(project))

    import django

    django.setup()
    from django.db import connection, models, router
    from django.db.models.signals import pre_save
    from orders.models import Order, OrderItem
    from orders.serializers import OrderItemSerializer, OrderSerializer
    from rest_framework.test import APIClient

    from sanka_extension_drf_to_fastapi.django_fastapi import (
        _build_serializer_ir,
        _create_payload,
        _field_payload,
    )
    from sanka_extension_drf_to_fastapi.native_async import render_async_sql_files

    with connection.schema_editor() as schema:
        schema.create_model(Order)
        schema.create_model(OrderItem)
    order = Order.objects.create(reference="ORD-1", status="new")
    OrderItem.objects.create(order=order, sku="SKU-A", quantity=2, price="10.00")
    connection.close()
    shutil.copy2(source_db, target_db)

    ir = _build_serializer_ir(
        OrderSerializer, Order, name="orders.OrderSerializer", ordering=("id",), analyze_writes=True
    )
    assert ir.supported and ir.create_contract is not None

    def admitted(serializer=OrderSerializer):
        return _build_serializer_ir(
            serializer, Order, name="orders.OrderSerializer", ordering=("id",), analyze_writes=True
        ).supported

    def custom_save(self, *args, **kwargs):
        self.quantity = min(self.quantity, 10)
        return models.Model.save(self, *args, **kwargs)

    with patch.object(OrderItem, "save", custom_save):
        assert not admitted(), "model save logic cannot be discarded by native creation"

    class FilteredQuerySet(models.QuerySet):
        def create(self, **kwargs):
            kwargs["quantity"] = 1
            return super().create(**kwargs)

    with patch.object(OrderItem.objects, "_queryset_class", FilteredQuerySet):
        assert not admitted(), "manager create logic cannot be discarded"

    def change_quantity(sender, instance, **kwargs):
        instance.quantity = 1

    pre_save.connect(change_quantity, sender=OrderItem, weak=False)
    try:
        assert not admitted(), "model signals cannot be discarded"
    finally:
        pre_save.disconnect(change_quantity, sender=OrderItem)

    with patch.object(router, "routers", [object()]):
        assert not admitted(), "database routing requires manual adaptation"

    class OptionalItems(OrderSerializer):
        items = OrderItemSerializer(many=True, required=False)

    class NullableItems(OrderSerializer):
        items = OrderItemSerializer(many=True, allow_null=True)

    assert not admitted(OptionalItems), "an unconditional pop cannot omit optional children"
    assert not admitted(NullableItems), "nullable child collection semantics are unsupported"
    assert admitted(), "plain models remain supported after checking custom behaviors"
    spec = {
        "model_class": ir.model_class,
        "db_table": ir.db_table,
        "pk_attname": ir.pk_attname,
        "ordering": list(ir.ordering),
        "lookup": ir.lookup,
        "fields": [_field_payload(field) for field in ir.fields],
        "create": _create_payload(ir),
        "routes": [{"path": "/api/orders/", "operation": "create", "method": "POST"}],
    }
    manifest = {
        "resources": [spec],
        "allow": {"/api/orders/": "GET, POST, HEAD, OPTIONS"},
        "database": {"vendor": "sqlite", "name": str(target_db)},
    }
    (generated / "sanka-manifest.json").write_text(json.dumps(manifest))
    render_async_sql_files(
        lambda name, content: (generated / name).write_text(content),
        entrypoint="app.py",
        manifest=manifest,
        sql_engine="tortoise",
    )

    def body(reference, quantities):
        return {
            "reference": reference,
            "items": [
                {"sku": f"SKU-{index}", "quantity": quantity, "price": "4.50"}
                for index, quantity in enumerate(quantities)
            ],
        }

    cases = [
        (body("ORD-2", [3, 4]), 201),
        (body("ORD-100", [60, 40]), 201),
        (body("ORD-101", [60, 41]), 400),
        (body("ORD-120", [60, 60]), 400),
        (body("ORD-ZERO", [1, 0]), 400),
        (body("ORD-1", []), 400),
    ]

    def snapshot(database):
        with sqlite3.connect(database) as db:
            return {
                table: db.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall()
                for table in ("orders_order", "orders_orderitem")
            }

    client = APIClient()
    source = []
    for payload, expected in cases:
        before = snapshot(source_db)
        response = client.post("/api/orders/", payload, format="json")
        assert response.status_code == expected
        after = snapshot(source_db)
        if expected == 400:
            assert after == before
        source.append((response.status_code, response.json(), after))
    connection.close()

    sys.path.insert(0, str(generated))
    import sanka_native
    import sanka_store
    from fastapi import Request

    async def target():
        await sanka_store.init_db()
        try:
            for (payload, expected), left in zip(cases, source, strict=True):
                before = snapshot(target_db)
                response = await sanka_native.handle(
                    spec,
                    "create",
                    Request({"type": "http", "path": "/api/orders/"}),
                    raw_body=json.dumps(payload).encode(),
                )
                after = snapshot(target_db)
                assert (response.status_code, json.loads(response.body), after) == left
                if expected == 400:
                    assert after == before

            # A persistence failure after one child was written must also roll back.
            original = sanka_store.create_row
            child_calls = 0

            async def fail_second_child(resource, data):
                nonlocal child_calls
                if resource["model_class"] == "OrderItem":
                    child_calls += 1
                    if child_calls == 2:
                        raise RuntimeError("injected child failure")
                return await original(resource, data)

            sanka_store.create_row = fail_second_child
            before = snapshot(target_db)
            try:
                await sanka_native.handle(
                    spec,
                    "create",
                    Request({"type": "http", "path": "/api/orders/"}),
                    raw_body=json.dumps(body("ORD-FAIL", [1, 1])).encode(),
                )
            except RuntimeError as error:
                assert str(error) == "injected child failure"
            else:
                raise AssertionError("expected child failure")
            assert snapshot(target_db) == before
        finally:
            await sanka_store.close_db()

    asyncio.run(target())
    print(json.dumps({"cases": len(cases), "rollback_after_insert": True}))


if __name__ == "__main__":
    main()

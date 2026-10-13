# SPDX-License-Identifier: Apache-2.0
"""Published capture must retain nested transactional replacement contracts."""

import dataclasses
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from sanka_extension_python_to_golang.adapter import handle
from test_python_to_golang import request

FIXTURE = Path(__file__).parent / "fixtures/nested_shipments"


def project(tmp_path):
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    return dataclasses.replace(
        request(tmp_path, "drf", "fiber"),
        command="scan",
        configuration={
            "source_framework": "drf",
            "target_framework": "fiber",
            "source_file": "shop_config/urls.py",
            "models_file": "orders/models.py",
            "database_layer": "sqlite",
        },
    )


@pytest.mark.parametrize("annotated", [False, True])
def test_nested_write_capture(tmp_path, annotated):
    base = project(tmp_path)
    if annotated:
        path = tmp_path / "orders/serializers.py"
        path.write_text(
            "from __future__ import annotations\nfrom typing import Any\n"
            + path.read_text()
            .replace(
                "def to_internal_value(self, data):",
                "def to_internal_value(self, data: Any) -> dict[str, Any]:",
            )
            .replace(
                "def create(self, validated_data):",
                "def create(self, validated_data: dict[str, Any]) -> Shipment:",
            )
        )
    scanned = handle(base)
    assert scanned.outcome == "success", scanned.error
    assert scanned.data["generation_ready"], scanned.data["gaps"]
    models = {m["name"]: m for m in scanned.data["models"]}
    assert models["Parcel"]["constraints"][0]["columns"] == ["shipment_id", "label"]
    assert models["Charge"]["constraints"][0]["columns"] == ["parcel_id", "code"]


@pytest.mark.slow
@pytest.mark.skipif(os.getenv("SANKA_GO_TESTS") != "1", reason="requires cached Go toolchain")
@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_nested_writes_replay(tmp_path, monkeypatch, target):
    base = project(tmp_path)
    base = dataclasses.replace(
        base, configuration=base.configuration | {"target_framework": target}
    )
    monkeypatch.setenv("SANKA_GO_SOURCE_PYTHON", sys.executable)
    monkeypatch.delenv("SANKA_GO_TARGET_TEST_DATABASE_URL", raising=False)
    parcel = {"label": "box", "quantity": 2, "charges": [{"code": "packing", "amount": "2.35"}]}
    create = {
        "method": "POST",
        "path": "/api/orders/",
        "body": {"reference": "ship-a", "parcels": [parcel]},
    }
    scenarios = [dict(create, id="create", expected_status=201)]
    for name, body, status in [
        ("preserve", {"memo": "updated"}, 200),
        ("clear", {"parcels": []}, 200),
        ("replace", {"parcels": [dict(parcel, label="new")]}, 200),
        ("duplicate-parent", {"parcels": [parcel, parcel]}, 400),
        ("duplicate-leaf", {"parcels": [dict(parcel, charges=parcel["charges"] * 2)]}, 400),
        ("required-despite-patch", {"parcels": [{"label": "incomplete"}]}, 400),
        ("decimal", {"parcels": [dict(parcel, charges=[{"code": "p", "amount": "1.234"}])]}, 400),
        (
            "negative-decimal",
            {"parcels": [dict(parcel, charges=[{"code": "p", "amount": "-12.30"}])]},
            200,
        ),
        ("missing-grandchildren", {"parcels": [{"label": "box", "quantity": 2}]}, 400),
        ("bad-grandchild", {"parcels": [dict(parcel, charges=[{}])]}, 400),
        ("bad-list", {"parcels": {}}, 400),
        ("bad-child", {"parcels": [None]}, 400),
        ("string-quantity", {"parcels": [dict(parcel, quantity="3")]}, 200),
    ]:
        write = {
            "method": "PATCH",
            "path": "/api/orders/1/",
            "body": body,
            "expected_status": status,
        }
        scenarios.append(dict(write, id=name, setup=[create]))
        scenarios.append(
            {
                "id": name + "-state",
                "setup": [create, write],
                "method": "GET",
                "path": "/api/orders/1/",
                "expected_status": 200,
            }
        )
    replacement = {
        "method": "PUT",
        "path": "/api/orders/1/",
        "body": {"reference": "put", "parcels": [dict(parcel, label="replacement")]},
        "expected_status": 200,
    }
    scenarios.append(dict(replacement, id="put-replacement", setup=[create]))
    scenarios.append(
        dict(
            replacement,
            id="put-missing-list",
            setup=[create],
            body={"reference": "put"},
            expected_status=400,
        )
    )
    scenarios.append(
        {
            "id": "list-recursive",
            "method": "GET",
            "path": "/api/orders/",
            "setup": [create],
            "expected_status": 200,
        }
    )
    for name, body in [
        ("duplicate-parent", [parcel, parcel]),
        ("duplicate-leaf", [dict(parcel, charges=parcel["charges"] * 2)]),
    ]:
        failed = dict(create, body={"reference": "bad", "parcels": body}, expected_status=400)
        scenarios.append(dict(failed, id="create-" + name))
        scenarios.append(
            dict(create, id="identity-after-" + name, setup=[failed], expected_status=201)
        )
    (tmp_path / "sanka-verify.json").write_text(json.dumps({"scenarios": scenarios}))
    planned = handle(dataclasses.replace(base, command="plan"))
    assert planned.outcome == "success", planned.error
    applied = handle(
        dataclasses.replace(
            base,
            command="apply",
            reviewed_plan_hash="offline-fixture",
            configuration=base.configuration | {"extension_plan_hash": planned.data["plan_hash"]},
        )
    )
    assert applied.outcome == "success", applied.error
    verified = handle(dataclasses.replace(base, command="verify"))
    assert verified.outcome == "success", verified.error
    assert verified.data["ok"], verified.data


@pytest.mark.parametrize(
    "change",
    [
        "no-atomic",
        "unhandled-write",
        "different-delete",
        "unique-drift",
        "eager-annotation",
        "recursive",
        "extra-constraint",
        "uncaptured-prefetch",
        "missing-patch-guard",
        "postgres-destination",
        "shadow-index",
    ],
)
def test_uncaptured_nested_semantics_block_generation(tmp_path, change):
    base = project(tmp_path)
    path = tmp_path / "orders/serializers.py"
    source = path.read_text()
    if change == "shadow-index":
        source = source.replace(
            "except IntegrityError as exc:", "except IntegrityError as parcel_index:"
        ).replace("from exc", "from parcel_index")
    elif change == "postgres-destination":
        base = dataclasses.replace(
            base, configuration=base.configuration | {"database_layer": "pgx"}
        )
    elif change == "uncaptured-prefetch":
        path = tmp_path / "orders/views.py"
        source = path.read_text().replace("parcels__charges", "parcels__uncaptured")
    elif change == "missing-patch-guard":
        source = source.replace('("label", "quantity", "charges")', '("label", "quantity")')
    elif change == "no-atomic":
        source = source.replace(
            "with transaction.atomic():", "with transaction.atomic(savepoint=False):"
        )
    elif change == "unhandled-write":
        source = source.replace(
            "instance.save()", "instance.save()\n            instance.parcels.all().delete()"
        )
    elif change == "different-delete":
        source = source.replace(
            "instance.parcels.all().delete()", "instance.parcels.filter(quantity=1).delete()"
        )
    elif change == "unique-drift":
        path = tmp_path / "orders/models.py"
        source = path.read_text().replace('("shipment", "label")', '("shipment", "quantity")')
    elif change == "eager-annotation":
        source = source.replace(
            "def create(self, validated_data):",
            "def create(self, validated_data) -> side_effect():",
        )
    elif change == "recursive":
        source = source.replace(
            "charges = ChargeSerializer(many=True)", "charges = ParcelSerializer(many=True)"
        )
    else:
        # A second constraint must not silently share the first constraint's error mapping.
        for name in ["orders/models.py", "orders/migrations/0001_initial.py"]:
            file = tmp_path / name
            file.write_text(
                file.read_text().replace(
                    '[("shipment", "label")]', '[("shipment", "label"), ("shipment", "quantity")]'
                )
            )
    path.write_text(source)
    scanned = handle(base)
    assert scanned.outcome == "success", scanned.error
    assert not scanned.data["generation_ready"]
    assert scanned.data["gaps"]

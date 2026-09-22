# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""Conventional Django projects must retain serializer and transaction semantics."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from sanka_extension_python_to_golang.capture import capture, configuration
from sanka_extension_python_to_golang.drf_replay import replay_project, scenario_groups
from sanka_extension_python_to_golang.render import render

FIXTURE = Path(__file__).parent / "fixtures/drf_project"


def project(tmp_path):
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    return configuration(
        {
            "source_framework": "drf",
            "source_file": "shop_config/urls.py",
            "models_file": "orders/models.py",
            "database_layer": "pgx",
        }
    )


@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_conventional_drf_capture(tmp_path, target):
    config = project(tmp_path) | {"target_framework": target}
    result = capture(tmp_path, config)
    assert result["gaps"] == []
    assert result["generation_ready"]
    assert result == capture(tmp_path, config)
    assert {model["table"] for model in result["models"]} == {"orders_order", "orders_orderitem"}
    contract = result["drf_project"]
    assert contract["settings_module"] == "shop_config.settings"
    assert contract["database"]["engine"] == "sqlite"
    view = contract["views"][0]
    assert view["path"] == "/api/orders/"
    assert view["serializer"]["nested"]["aggregate"]["limit"] == 100
    assert view["serializer"]["nested"]["update"] == "ignore"


@pytest.mark.parametrize(
    "filename, old, new",
    [
        (
            "orders/views.py",
            "queryset = Order.objects.all()",
            "queryset = Order.objects.filter(status='paid')",
        ),
        ("orders/serializers.py", "return order", "order.memo = 'changed'\n        return order"),
        (
            "shop_config/settings.py",
            "MIDDLEWARE: list[str] = []",
            "MIDDLEWARE = ['custom.Middleware']",
        ),
    ],
)
def test_unknown_drf_behavior_blocks_generation(tmp_path, filename, old, new):
    config = project(tmp_path)
    path = tmp_path / filename
    assert old in path.read_text()
    path.write_text(path.read_text().replace(old, new))
    result = capture(tmp_path, config)
    assert result["gaps"]
    assert not result["generation_ready"]


@pytest.mark.parametrize(
    "filename,old,new",
    [
        (
            "orders/migrations/0001_initial.py",
            "max_length=30, unique=True",
            "max_length=31, unique=True",
        ),
        (
            "orders/migrations/0001_initial.py",
            "    initial = True",
            "    initial = True\n    atomic = False",
        ),
        (
            "orders/serializers.py",
            "from orders.models import Order, OrderItem",
            "from orders.models import Order",
        ),
        (
            "orders/serializers.py",
            "    items = OrderItemSerializer(many=True)",
            "    items = OrderItemSerializer(many=True)\n    items = OrderItemSerializer(many=True)",
        ),
    ],
)
def test_conventional_project_rejects_schema_and_binding_drift(tmp_path, filename, old, new):
    config = project(tmp_path)
    path = tmp_path / filename
    path.write_text(path.read_text().replace(old, new))
    assert capture(tmp_path, config)["gaps"]


@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_conventional_drf_renders_backend(tmp_path, target):
    result = capture(tmp_path, project(tmp_path) | {"target_framework": target})
    generated = render(result)
    assert "drf.go" in generated
    assert "cmd/api/main.go" in generated
    assert "migration_identity" in generated["migrations/00001_initial.sql"]
    assert "ON DELETE CASCADE" in generated["migrations/00001_initial.sql"]


@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_conventional_drf_native_validation(tmp_path, target):
    if os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("requires native Go")
    result = capture(tmp_path, project(tmp_path) | {"target_framework": target})
    output = tmp_path / ".sanka/candidate"
    for name, contents in render(result).items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    (output / "drf_validation_test.go").write_text(r"""package backend
import "testing"
func TestDRFValidation(t *testing.T) {
    schema := drfSchema.Project.Views[0].Serializer
    values, invalid := drfValidate([]byte(`{"reference":" A ","items":[{"sku":"x","quantity":"2","price":"1.00"}]}`),schema,false)
    if len(invalid)>0 || values["reference"]!="A" || len(values)!=2 { t.Fatalf("valid/defaults: %#v %#v", values,invalid) }
    _, invalid = drfValidate([]byte(`{"reference":"","items":[{"sku":"x","quantity":0,"price":"1.00"}]}`),schema,false)
    if len(invalid)!=2 { t.Fatalf("nested errors: %#v",invalid) }
    values, invalid = drfValidate([]byte(`{}`),schema,true)
    if len(invalid)>0 || len(values)>0 { t.Fatalf("partial defaults: %#v %#v",values,invalid) }
}
""")
    completed = subprocess.run(
        ["go", "test", "-mod=readonly", "-p=2", "./..."],
        cwd=output,
        env=os.environ | {"GOMAXPROCS": "2"},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_independent_scenario_setup_is_observed(tmp_path):
    captured = capture(tmp_path, project(tmp_path))
    groups = scenario_groups(tmp_path, captured)
    assert len(groups) == 7
    assert [len(group) for group in groups] == [2, 2, 1, 2, 2, 2, 2]
    assert groups[0][0]["method"] == "POST"
    assert groups[0][0]["expected_status"] == 201


@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_conventional_drf_sqlite_postgresql_parity(tmp_path, monkeypatch, target):
    dsn = os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN")
    if os.getenv("SANKA_GO_TESTS") != "1" or not dsn:
        pytest.skip("requires native Go and isolated PostgreSQL fixture")
    import uuid

    import psycopg
    from psycopg import sql
    from test_golang_schema import schema_dsn

    name = "drf_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            monkeypatch.setenv("SANKA_GO_TARGET_TEST_DATABASE_URL", schema_dsn(dsn, name))
            config = project(tmp_path) | {"target_framework": target}
            scenario_file = tmp_path / "sanka-verify.json"
            document = json.loads(scenario_file.read_text())
            for i, price in enumerate(
                ["1.234", "1234567.00", "123456789", "bad", "NaN", "0.001", None]
            ):
                document["scenarios"].append(
                    {
                        "id": f"decimal-{i}",
                        "method": "POST",
                        "path": "/api/orders/",
                        "expected_status": 400,
                        "body": {
                            "reference": "invalid",
                            "items": [{"sku": "a", "quantity": 1, "price": price}],
                        },
                    }
                )
            document["scenarios"].append(
                {
                    "id": "choice-whitespace",
                    "method": "POST",
                    "path": "/api/orders/",
                    "expected_status": 400,
                    "body": {"reference": "invalid", "status": " paid ", "items": []},
                }
            )
            setup = {
                "method": "POST",
                "path": "/api/orders/",
                "body": {"reference": "original", "status": "paid", "memo": "kept", "items": []},
            }
            document["scenarios"].append(
                {
                    "id": "put-optional-absent",
                    "setup": [setup],
                    "method": "PUT",
                    "path": "/api/orders/1/",
                    "expected_status": 200,
                    "body": {"reference": "replaced", "items": []},
                }
            )
            for i, body in enumerate(
                [
                    {},
                    {"reference": "x"},
                    {"reference": "x", "items": None},
                    {"reference": "x", "items": [None]},
                    {"reference": "x", "items": [{}, {"sku": "x", "quantity": 0, "price": "bad"}]},
                    {"reference": "x", "status": "", "items": []},
                ]
            ):
                document["scenarios"].append(
                    {
                        "id": f"validation-{i}",
                        "method": "POST",
                        "path": "/api/orders/",
                        "expected_status": 400,
                        "body": body,
                    }
                )
            rollback = dict(document["scenarios"][6])
            rollback.pop("setup")
            rollback.pop("id")
            document["scenarios"].append(
                {
                    "id": "create-after-rollback",
                    "setup": [setup, rollback],
                    "method": "POST",
                    "path": "/api/orders/",
                    "expected_status": 201,
                    "body": {
                        "reference": "after",
                        "items": [{"sku": "a", "quantity": 1, "price": "0.00"}],
                    },
                }
            )
            for i, choice in enumerate([True, {}, [], {"key": [True, None, 1.0]}, 1.0]):
                document["scenarios"].append(
                    {
                        "id": f"choice-type-{i}",
                        "method": "POST",
                        "path": "/api/orders/",
                        "expected_status": 400,
                        "body": {"reference": "choice", "status": choice, "items": []},
                    }
                )
            scenario_file.write_text(json.dumps(document))
            captured = capture(tmp_path, config)
            output = tmp_path / ".sanka/candidate"
            for filename, contents in render(captured).items():
                path = output / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents)
            report = replay_project(tmp_path, output, captured, "verify")
            assert report["ok"], report["failures"]
            assert report["source"] == report["candidate"]
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


@pytest.mark.parametrize(
    "case", ["annotation", "id-override", "nested-unique", "optional-aggregate"]
)
def test_review_capture_boundaries(tmp_path, case):
    config = project(tmp_path)
    model = tmp_path / "orders/models.py"
    serializer = tmp_path / "orders/serializers.py"
    migration = tmp_path / "orders/migrations/0001_initial.py"
    if case == "annotation":
        model.write_text(
            model.read_text().replace(
                "reference =", 'reference: setattr(reference, "max_length", 1) ='
            )
        )
    elif case == "id-override":
        serializer.write_text(
            serializer.read_text().replace(
                "class OrderSerializer(serializers.ModelSerializer):",
                "class OrderSerializer(serializers.ModelSerializer):\n    id = serializers.IntegerField(min_value=1)",
            )
        )
    elif case == "nested-unique":
        model.write_text(
            model.read_text().replace(
                "sku = models.CharField(max_length=30)",
                "sku = models.CharField(max_length=30, unique=True)",
            )
        )
        migration.write_text(
            migration.read_text().replace(
                '("sku", models.CharField(max_length=30))',
                '("sku", models.CharField(max_length=30, unique=True))',
            )
        )
    else:
        for path in [model, migration]:
            path.write_text(
                path.read_text().replace(
                    "models.PositiveIntegerField()", "models.PositiveIntegerField(default=1)"
                )
            )
        serializer.write_text(
            serializer.read_text().replace(
                "    quantity = serializers.IntegerField(min_value=1)\n", ""
            )
        )
    result = capture(tmp_path, config)
    assert result["gaps"] and not result["generation_ready"]


def test_explicit_integer_override_replaces_model_validation(tmp_path):
    config = project(tmp_path)
    for name in ["orders/models.py", "orders/migrations/0001_initial.py"]:
        path = tmp_path / name
        path.write_text(
            path.read_text().replace(
                "models.PositiveIntegerField()",
                "models.PositiveIntegerField(default=1, unique=True)",
            )
        )
    captured = capture(tmp_path, config)
    assert not captured["gaps"]
    fields = captured["drf_project"]["views"][0]["serializer"]["nested"]["serializer"]["fields"]
    field = next(f for f in fields if f["name"] == "quantity")
    assert field["required"] is True and field["unique"] is False and "default" not in field

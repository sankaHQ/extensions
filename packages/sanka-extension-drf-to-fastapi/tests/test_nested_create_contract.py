# SPDX-License-Identifier: Apache-2.0
"""Source-derived rule lowering and direct generated-handler acceptance."""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sanka_extension_drf_to_fastapi.nested_create import lower_nested_create

FIXTURE = Path(__file__).parent / "fixtures" / "order_tracker"
SOURCE = ast.unparse(
    next(
        node
        for node in ast.walk(ast.parse((FIXTURE / "orders/serializers.py").read_text()))
        if isinstance(node, ast.FunctionDef) and node.name == "create"
    )
)


def lower(source):
    return lower_nested_create(
        source,
        parent_aliases={"Order"},
        child_aliases={"OrderItem"},
        transaction_aliases={"transaction"},
        error_aliases={"serializers.ValidationError"},
        field="items",
        foreign_key="order",
        integer_fields={"quantity"},
    )


def test_lowering_preserves_source_rule_and_derives_changed_constants():
    contract = lower(SOURCE)
    assert contract == {
        "style": "nested",
        "atomic": True,
        "rule": {
            "field": "items",
            "attribute": "quantity",
            "operator": "Gt",
            "limit": 100,
            "detail": {"items": ["Order exceeds 100 total units."]},
        },
    }
    changed = lower(SOURCE.replace("100", "27").replace(" > ", " >= "))
    assert changed["rule"]["limit"] == 27
    assert changed["rule"]["operator"] == "GtE"
    assert changed["rule"]["detail"] == {"items": ["Order exceeds 27 total units."]}


@pytest.mark.parametrize(
    "old,new",
    [
        ("return order", "print(order)\n    return order"),
        (".all()", ".filter(quantity=1)"),
        ("total > 100", "total > 100 and False"),
        ("sum(", "max("),
        ("Order.objects.create", "Order.objects.get_or_create"),
        ("transaction.atomic()", "transaction.atomic(savepoint=False)"),
        ("order=order", "order_id=1"),
        ("**item_data", "**validated_data"),
        ("quantity for item", "price for item"),
        (
            "def create(self, validated_data):",
            "@transaction.atomic\ndef create(self, validated_data):",
        ),
    ],
)
def test_unimplemented_statements_and_semantics_fail_closed(old, new):
    assert old in SOURCE
    assert lower(SOURCE.replace(old, new)) is None


def test_exact_pinned_order_tracker_fixture():
    provenance = json.loads((FIXTURE / "provenance.json").read_text())
    assert provenance["commit"] == "5d27fbcb6759c1fc0bfd0cc556e570851a62ab6d"
    for path, digest in provenance["files"].items():
        assert hashlib.sha256((FIXTURE / path).read_bytes()).hexdigest() == digest


def test_source_and_generated_handler_preserve_order_tracker_rules(tmp_path):
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)
    probe = Path(__file__).with_name("order_tracker_contract_probe.py")
    result = subprocess.run(
        [sys.executable, str(probe), str(project)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {"cases": 6, "rollback_after_insert": True}


def sample_scan(contract, *, schema_version=8):
    from sanka_extension_drf_to_fastapi.model import (
        DatabaseIR,
        FrameworkScan,
        RouteIR,
        SerializerIR,
    )

    return FrameworkScan(
        schema_version=schema_version,
        source=".",
        language="python",
        framework="django-rest-framework",
        python_version="3.12",
        django_version="5.2",
        drf_version="3.18",
        settings_module="settings",
        root_urlconf="urls",
        database=DatabaseIR(vendor="sqlite", name="source.sqlite3"),
        routes=(
            RouteIR(
                method="POST",
                path="/orders/",
                operation="create",
                view="Orders",
                serializer="OrderSerializer",
                native=True,
            ),
        ),
        serializer_details=(
            SerializerIR(
                name="OrderSerializer",
                model="orders.Order",
                model_module="orders",
                model_class="Order",
                object_name="Order",
                create_style="carryover",
                create_source=SOURCE,
                create_contract=contract,
            ),
        ),
    ).with_hash()


@pytest.mark.parametrize(
    "engine,contract,automatic",
    [
        ("tortoise", lower(SOURCE), True),
        ("sqlalchemy", lower(SOURCE), False),
        ("tortoise", None, False),
    ],
)
def test_plan_refuses_unimplemented_store_or_legacy_custom_contract(
    tmp_path, monkeypatch, engine, contract, automatic
):
    from sanka_extension_drf_to_fastapi import django_fastapi

    monkeypatch.setattr(
        django_fastapi, "load_framework_scan", lambda *args, **kwargs: sample_scan(contract)
    )
    plan = django_fastapi.plan_fastapi(tmp_path, sql_engine=engine)
    assert plan.routes[0].automatic is automatic
    if not automatic:
        assert (
            plan.routes[0].adaptation_reasons[0].code == "SANKA_DRF_NESTED_TRANSACTION_UNSUPPORTED"
        )


def test_new_contract_is_hash_bound_without_changing_legacy_scan_hash():
    from sanka_extension_drf_to_fastapi.hashing import content_hash
    from sanka_extension_drf_to_fastapi.model import FrameworkScan

    legacy = sample_scan(None, schema_version=7).to_dict()
    legacy.pop("scan_hash")
    legacy["serializer_details"][0].pop("create_contract")
    assert FrameworkScan.from_dict(legacy).with_hash().scan_hash == content_hash(legacy)
    original = sample_scan(lower(SOURCE))
    changed = lower(SOURCE.replace("100", "101"))
    assert sample_scan(changed).scan_hash != original.scan_hash


def test_parser_still_fails_closed_when_python_assertions_are_disabled():
    script = (
        "from sanka_extension_drf_to_fastapi.nested_create import lower_nested_create; "
        f"result=lower_nested_create({SOURCE!r},parent_aliases=set(),child_aliases={{'OrderItem'}},"
        "transaction_aliases={'transaction'},error_aliases={'serializers.ValidationError'},"
        "field='items',foreign_key='order',integer_fields={'quantity'}); "
        "raise SystemExit(0 if result is None else 1)"
    )
    assert subprocess.run([sys.executable, "-O", "-c", script], timeout=10).returncode == 0


def test_generated_tests_use_provided_interpreter_without_installing(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from sanka_extension_drf_to_fastapi import fastapi_tests

    monkeypatch.setattr(
        fastapi_tests,
        "load_framework_scan",
        lambda *args, **kwargs: SimpleNamespace(scan_hash="scan"),
    )
    monkeypatch.setattr(
        fastapi_tests,
        "load_fastapi_plan",
        lambda *args, **kwargs: SimpleNamespace(
            plan_hash="plan", default_output="target", mode="native"
        ),
    )
    monkeypatch.setattr(fastapi_tests, "_isolated_env", lambda *args: ({}, False))
    monkeypatch.setattr(fastapi_tests, "_render_generated_tests", lambda *args, **kwargs: "pass\n")

    def unexpected_install(*args, **kwargs):
        raise AssertionError("an explicit interpreter must not install an environment")

    monkeypatch.setattr(fastapi_tests, "ensure_generated_environment", unexpected_install)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="Ran 1 test\n", stderr="")

    monkeypatch.setattr(fastapi_tests.subprocess, "run", run)
    output = tmp_path / "target"
    output.mkdir()
    (output / "sanka-manifest.json").write_text(
        json.dumps({"source_scan_hash": "scan", "plan_hash": "plan"})
    )
    result = fastapi_tests.test_fastapi_app(tmp_path, candidate_python=sys.executable)
    assert result["ok"] and result["provided_environment"]
    assert result["environment"] is None
    assert calls[0][0] == sys.executable

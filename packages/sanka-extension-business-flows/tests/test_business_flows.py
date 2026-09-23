# SPDX-License-Identifier: Apache-2.0
"""Business selection invariants; no provider calls or native execution."""

from dataclasses import replace

import pytest

from sanka_extension_business_flows import (
    capability,
    catalog,
    definition,
    generate,
    recipe,
    resolve_parameters,
)
from sanka_extension_business_flows.generator import RECIPE_ID, WORKFLOW_ID
from sanka_extensions.flow import (
    ArtifactIdentity,
    BlueprintRequest,
    FlowDefinition,
    NativeOrderBillingWorkflow,
    TargetIdentity,
)


def parameters():
    return {
        "source_endpoint_id": "11111111-1111-4111-8111-111111111111",
        "mapping_template_id": "saved-order-mapping",
        "pipeline_id": "default",
        "deal_stage_ids": ["closedwon"],
        "updated_since": "2026-09-01",
    }


def selected_definition():
    return definition(
        parameters(), mapping=ArtifactIdentity("saved-order-mapping", "7", "sha256:" + "a" * 64)
    )


def request(*, selected=None, capabilities=None, extension=None, template=None):
    selected = selected or selected_definition()
    profile = NativeOrderBillingWorkflow.from_configuration(
        WORKFLOW_ID, selected.parameters["native_configuration"]
    )
    return BlueprintRequest(
        "request-one",
        extension or ArtifactIdentity("sanka/business-flows", "0.1.0a2", "sha256:" + "b" * 64),
        selected,
        TargetIdentity(
            "workspace-one",
            "1",
            (
                (
                    *profile.required_capabilities,
                    "flow.resource.workflow/v1",
                    "sanka-flow-blueprint/v3",
                )
                if capabilities is None
                else capabilities
            ),
        ),
        (),
        {},
        template=template or capability().template,
        output_schema="sanka-flow-blueprint/v3",
    )


def test_catalog_has_all_categories_and_honest_generation_support():
    data = catalog()
    assert len(data["recipes"]) == 28
    assert {c["id"] for c in data["categories"]} == {
        "sales",
        "billing",
        "purchasing",
        "inventory",
        "expenses",
        "projects",
        "support",
        "hr",
        "cross-department",
    }
    assert len({r["id"] for r in data["recipes"]}) == 28
    assert {r["id"] for r in data["recipes"] if r["generation"] != "definition_only"} == {RECIPE_ID}
    assert all(r["title"] and r["title_ja"] and r["version"] == 1 for r in data["recipes"])
    assert all("business_type" not in {p["key"] for p in r["parameters"]} for r in data["recipes"])


@pytest.mark.parametrize("item", catalog()["recipes"], ids=lambda item: item["id"])
def test_every_definition_exposes_valid_defaults_and_rejects_unknown_ai_fields(item):
    assert resolve_parameters(item["id"], {}, require_complete=False) == {
        p["key"]: p["default"] for p in item["parameters"]
    }
    with pytest.raises(ValueError, match="Unknown template"):
        resolve_parameters(item["id"], {"invented_ai_option": True})


def test_metadata_and_resolved_lists_are_independent_copies():
    first = recipe(RECIPE_ID)
    first["parameters"].clear()
    assert recipe(RECIPE_ID)["parameters"]
    source = parameters()
    source["deal_stage_ids"] = ["closedwon", "closedwon"]
    values = resolve_parameters(RECIPE_ID, source)
    values["deal_stage_ids"].append("other")
    assert source["deal_stage_ids"] == ["closedwon", "closedwon"]


def test_configuration_produces_exact_stable_inactive_order_billing_definition():
    first = generate(request())
    second = generate(request())
    assert first.to_dict() == second.to_dict()
    profile = NativeOrderBillingWorkflow.from_dict(first.blueprint.resources[0].spec)
    assert profile.interval_minutes == profile.invoice_due_days == 30
    assert profile.mapping == ArtifactIdentity("saved-order-mapping", "7", "sha256:" + "a" * 64)
    assert profile.source_endpoint_id == parameters()["source_endpoint_id"]
    assert profile.to_dict()["policies"] == {
        "import_target": "order",
        "import_completion": "complete_only",
        "invoice_source": "imported_orders_only",
        "invoice_status": "draft",
        "existing_invoice": "preserve",
        "invoice_grouping": "one_per_order",
        "construction": "inactive",
    }
    assert first.blueprint.scenarios == ()
    assert first.blueprint.origin.identity == capability().template


@pytest.mark.parametrize(
    "key,value",
    [
        ("interval_minutes", True),
        ("interval_minutes", 1),
        ("invoice_due_days", 0),
        ("deal_stage_ids", []),
        ("updated_since", "2026-02-30"),
    ],
)
def test_manual_and_ai_configuration_reject_invalid_values(key, value):
    settings = parameters() | {key: value}
    with pytest.raises(ValueError):
        definition(
            settings, mapping=ArtifactIdentity("saved-order-mapping", "7", "sha256:" + "a" * 64)
        )


def test_host_cannot_substitute_resolved_mapping():
    with pytest.raises(ValueError, match="selected mapping"):
        definition(
            parameters(), mapping=ArtifactIdentity("another-mapping", "7", "sha256:" + "a" * 64)
        )


def test_raw_sdk_request_does_not_bypass_recipe_ranges():
    fields = selected_definition().parameters["native_configuration"] | {"interval_minutes": 1}
    selected = FlowDefinition(type=capability().type, parameters={"native_configuration": fields})
    with pytest.raises(ValueError, match="supported range"):
        generate(request(selected=selected))


def test_missing_independent_host_capability_is_rejected():
    with pytest.raises(ValueError, match="capabilities"):
        generate(request(capabilities=()))


def test_changed_template_or_extension_identity_is_rejected():
    with pytest.raises(ValueError, match="declared template"):
        generate(request(template=replace(capability().template, digest="sha256:" + "c" * 64)))
    with pytest.raises(ValueError, match="exact installed extension"):
        generate(
            request(
                extension=ArtifactIdentity("sanka/business-flows", "0.1.0a1", "sha256:" + "b" * 64)
            )
        )

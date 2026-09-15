# SPDX-License-Identifier: Apache-2.0
"""Reviewable recipe expectations and protocol boundaries, without native writes."""

import copy
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from sanka_extension_business_flows import catalog, resolve_parameters
from sanka_extension_business_flows.native_generator import (
    CANDIDATE_EXTENSION_VERSION,
    generate_native,
    native_capability,
    native_definition,
    native_profile,
)
from sanka_extension_business_flows.native_recipes import NATIVE_RECIPES, native_recipe
from sanka_extensions.flow import (
    ArtifactIdentity,
    BlueprintRequest,
    BlueprintResponse,
    FlowDefinition,
    TargetIdentity,
    artifact_digest,
)
from sanka_extensions.flow.native_profiles import decode_native_workflow

FIXTURES = Path(__file__).parent / "fixtures" / "native_recipes"
CASES = [json.loads(path.read_text()) for path in sorted(FIXTURES.glob("*.json"))]


def request(case, *, selected=None, extension=None, template=None, capabilities=None):
    selected = selected or native_definition(case["recipe_id"], case["input"])
    profile = decode_native_workflow(selected.parameters["native_workflow"])
    return BlueprintRequest(
        "recipe-request",
        extension
        or ArtifactIdentity(
            "sanka/business-flows", CANDIDATE_EXTENSION_VERSION, "sha256:" + "b" * 64
        ),
        selected,
        TargetIdentity(
            "test-workspace",
            "reviewed-revision",
            capabilities
            if capabilities is not None
            else (
                *profile.required_capabilities,
                "flow.resource.workflow/v1",
                "sanka-flow-blueprint/v5",
            ),
        ),
        (),
        {},
        template=template or native_capability(case["recipe_id"]).template,
        output_schema="sanka-flow-blueprint/v5",
    )


def example(recipe_id):
    return copy.deepcopy(next(case for case in CASES if case["recipe_id"] == recipe_id))


def test_each_remaining_recipe_has_one_independent_review_fixture_and_unique_binding():
    expected = {recipe["id"] for recipe in catalog()["recipes"]} - {"billing.hubspot-deal-invoices"}
    assert len(CASES) == len(NATIVE_RECIPES) == len(expected) == 27
    assert {case["recipe_id"] for case in CASES} == expected
    assert {selected.recipe_id for selected in NATIVE_RECIPES} == expected
    assert len({selected.selector for selected in NATIVE_RECIPES}) == 27
    assert len({selected.workflow_id for selected in NATIVE_RECIPES}) == 27


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["recipe_id"])
def test_recipe_generates_reviewed_settings_roles_and_fixed_policies(case):
    before = copy.deepcopy(case["input"])
    original_receipt_parameters = resolve_parameters(case["recipe_id"], case["input"])
    selected = request(case)
    response = generate_native(selected)
    assert generate_native(selected).to_dict() == response.to_dict()
    profile = decode_native_workflow(response.blueprint.resources[0].spec)
    assert profile.configuration == case["expected_configuration"]
    assert list(profile.node_ids) == case["expected_node_ids"]
    for key, value in case["policy_checks"].items():
        assert artifact_digest({key: profile.policies[key]}) == artifact_digest({key: value})
    assert profile.to_dict() == selected.definition.parameters["native_workflow"]
    assert response.blueprint.origin.identity == native_capability(case["recipe_id"]).template
    assert response.blueprint.scenarios == ()
    assert response.blueprint.schema_version == "sanka-flow-blueprint/v5"
    assert case["input"] == before
    assert resolve_parameters(case["recipe_id"], case["input"]) == original_receipt_parameters


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["recipe_id"])
def test_recipe_round_trips_in_isolated_installed_package_outside_repository(case, tmp_path):
    # Exercise the candidate generator in an isolated process without modifying
    # the published a1 executable, manifest or discovery capabilities.
    script = """
import json, sys
from sanka_extension_business_flows.native_generator import generate_native
from sanka_extensions.flow import BlueprintRequest
request = BlueprintRequest.from_dict(json.load(sys.stdin))
print(json.dumps(generate_native(request).to_dict()))
"""
    selected = request(case)
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        input=json.dumps(selected.to_dict()),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        timeout=20,
        check=True,
    )
    parsed = BlueprintResponse.from_dict(json.loads(result.stdout))
    parsed.validate_for(selected)
    assert parsed.to_dict() == generate_native(selected).to_dict()
    assert result.stderr == ""


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["recipe_id"])
def test_one_missing_recipe_execution_capability_rejects_generation_request(case):
    profile = native_profile(case["recipe_id"], case["input"])
    missing = profile.required_capabilities[-1]
    capabilities = (
        *profile.required_capabilities[:-1],
        "flow.resource.workflow/v1",
        "sanka-flow-blueprint/v5",
    )
    with pytest.raises(ValueError, match="capabilities"):
        generate_native(request(case, capabilities=capabilities))
    assert missing not in capabilities


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["recipe_id"])
def test_user_configuration_changes_keep_workflow_and_node_logical_ids(case):
    before = native_profile(case["recipe_id"], case["input"])
    settings = copy.deepcopy(case["expected_configuration"])
    parameters = settings.get("settings", {"approver_ids": settings.get("approver_ids")})
    if not parameters:
        assert case["recipe_id"] == "sales.deal-to-estimate"
        return
    for key in parameters:
        if key in {"assignee_ids", "approver_ids"}:
            parameters[key] = ["503"]
            break
        if type(parameters[key]) is int:
            parameters[key] += 1
            break
        if type(parameters[key]) is bool:
            parameters[key] = not parameters[key]
            break
        if key in {"won_stage", "invoice_notes"}:
            parameters[key] = "New selection"
            break
    after = native_profile(case["recipe_id"], parameters)
    assert before.configuration_digest != after.configuration_digest
    assert before.id == after.id
    assert before.node_ids == after.node_ids


def test_raw_protocol_cannot_select_a_different_recipe_or_logical_identity():
    case = example("billing.overdue-reminders")
    other = native_definition("purchasing.supplier-payment-reminders", {"assignee_ids": ["502"]})
    mismatched = FlowDefinition(
        type=native_recipe(case["recipe_id"]).selector, parameters=other.parameters
    )
    with pytest.raises(ValueError):
        generate_native(request(case, selected=mismatched))
    profile = native_profile(case["recipe_id"], case["input"])
    changed = type(profile).from_configuration("another-workflow", profile.configuration)
    definition = FlowDefinition(
        type=native_recipe(case["recipe_id"]).selector,
        parameters={"native_workflow": changed.to_dict()},
    )
    with pytest.raises(ValueError, match="canonical profile"):
        generate_native(request(case, selected=definition))


def test_raw_protocol_must_already_expose_effective_ticket_assignment_pool():
    case = example("support.ticket-assignment")
    profile = native_profile(case["recipe_id"], case["input"])
    changed = type(profile).from_configuration(
        profile.id, {"process": "ticket_assignment", "settings": {"assignee_ids": ["502", "501"]}}
    )
    selected = FlowDefinition(
        type=native_recipe(case["recipe_id"]).selector,
        parameters={"native_workflow": changed.to_dict()},
    )
    with pytest.raises(ValueError, match="canonical profile"):
        generate_native(request(case, selected=selected))


def test_changed_package_or_template_identity_is_rejected():
    case = CASES[0]
    selected = request(case)
    with pytest.raises(ValueError, match="exact candidate extension"):
        generate_native(request(case, extension=replace(selected.extension, revision="0.1.0a1")))
    with pytest.raises(ValueError, match="declared template"):
        generate_native(
            request(case, template=replace(selected.template, digest="sha256:" + "c" * 64))
        )


def test_candidate_bindings_do_not_claim_published_discovery_or_readiness():
    assert {
        recipe["id"] for recipe in catalog()["recipes"] if recipe["generation"] != "definition_only"
    } == {"billing.hubspot-deal-invoices"}
    with pytest.raises(ValueError, match="Unknown native"):
        native_capability("billing.hubspot-deal-invoices")
    with pytest.raises(ValueError, match="Unknown native"):
        native_definition("made-up-recipe", {})

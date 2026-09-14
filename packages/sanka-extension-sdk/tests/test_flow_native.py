# SPDX-License-Identifier: Apache-2.0
"""Native contracts preserve exact import/billing meaning across wire boundaries."""

from copy import deepcopy

import pytest

from sanka_extensions.flow import (
    ArtifactIdentity,
    Blueprint,
    BlueprintOrigin,
    NativeOrderBillingWorkflow,
    Resource,
)


def workflow():
    return NativeOrderBillingWorkflow(
        "billing",
        30,
        "11111111-1111-4111-8111-111111111111",
        ArtifactIdentity("saved-order-mapping", "7", "sha256:" + "a" * 64),
        "default",
        ("closedwon",),
        "2026-09-01",
        30,
    )


def blueprint():
    identity = ArtifactIdentity("sanka/hubspot-deal-invoices", "1", "sha256:" + "b" * 64)
    return Blueprint(
        id="billing",
        revision="1",
        origin=BlueprintOrigin("template", identity),
        extension=ArtifactIdentity("sanka/business-flows", "1", "sha256:" + "c" * 64),
        resources=(Resource("billing", "workflow", workflow().to_dict()),),
        schema_version="sanka-flow-blueprint/v3",
    )


def test_native_round_trip_and_capability_admission():
    candidate = blueprint()
    assert Blueprint.from_dict(candidate.to_dict()).digest == candidate.digest
    with pytest.raises(ValueError, match="capabilities"):
        candidate.require_supported()
    candidate.require_supported(capabilities=frozenset(candidate.required_capabilities))
    assert "flow.native.complete-import-output/v1" in candidate.required_capabilities
    assert "flow.native.preserve-existing-invoices/v1" in candidate.required_capabilities


@pytest.mark.parametrize(
    "policy,value",
    [
        ("import_target", "invoice"),
        ("import_completion", "partial_allowed"),
        ("invoice_source", "all_orders"),
        ("invoice_status", "sent"),
        ("existing_invoice", "overwrite"),
        ("construction", "active"),
    ],
)
def test_native_rejects_changes_to_business_safety(policy, value):
    payload = workflow().to_dict()
    payload["policies"][policy] = value
    with pytest.raises(ValueError, match="policies"):
        NativeOrderBillingWorkflow.from_dict(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("interval_minutes", True),
        ("interval_minutes", 0),
        ("invoice_due_days", -1),
        ("source_endpoint_id", "not-a-workspace-endpoint"),
        ("deal_stage_ids", []),
        ("deal_stage_ids", ["won", "won"]),
        ("updated_since", "2026-02-30"),
        ("updated_since", "20260901"),
    ],
)
def test_native_rejects_invalid_configuration(field, value):
    payload = workflow().to_dict()
    payload[field] = value
    with pytest.raises(ValueError):
        NativeOrderBillingWorkflow.from_dict(payload)


def test_native_has_no_arbitrary_action_or_reordered_steps():
    payload = workflow().to_dict()
    payload["node_ids"].reverse()
    with pytest.raises(ValueError, match="ordering"):
        NativeOrderBillingWorkflow.from_dict(payload)
    payload = workflow().to_dict()
    payload["script"] = "arbitrary code"
    with pytest.raises(ValueError, match="unexpected"):
        NativeOrderBillingWorkflow.from_dict(payload)


def test_native_blueprint_cannot_downgrade_or_hide_required_capabilities():
    payload = blueprint().to_dict()
    payload["required_capabilities"] = []
    with pytest.raises(ValueError, match="required_capabilities"):
        Blueprint.from_dict(payload)
    for version in ("sanka-flow-blueprint/v1", "sanka-flow-blueprint/v2"):
        changed = deepcopy(blueprint().to_dict())
        changed["schema_version"] = version
        if version.endswith("v1"):
            changed.pop("required_capabilities")
        with pytest.raises(ValueError):
            Blueprint.from_dict(changed)


def test_native_spec_is_immutable_after_construction():
    payload = workflow().to_dict()
    resource = Resource("billing", "workflow", payload)
    payload["interval_minutes"] = 1
    returned = resource.spec
    returned["interval_minutes"] = 2
    assert resource.spec["interval_minutes"] == 30


def request_and_result():
    from sanka_extensions.flow import BlueprintRequest, FlowDefinition, TargetIdentity

    candidate = blueprint()
    request = BlueprintRequest(
        "native-request",
        candidate.extension,
        FlowDefinition(
            type=candidate.origin.identity.id,
            parameters={"native_configuration": workflow().configuration},
        ),
        TargetIdentity("workspace-one", "r1", candidate.required_capabilities),
        (),
        {},
        template=candidate.origin.identity,
        output_schema="sanka-flow-blueprint/v3",
    )
    payload = candidate.to_dict()
    payload["parameters"] = request.blueprint_parameters
    return request, Blueprint.from_dict(payload)


def test_native_protocol_correlates_exact_executable_configuration():
    from sanka_extensions.flow import (
        BlueprintRequest,
        BlueprintResponse,
        decode_message,
        encode_message,
    )

    request, candidate = request_and_result()
    response = BlueprintResponse.success(request, candidate)
    response.validate_for(BlueprintRequest.from_dict(decode_message(encode_message(request))))
    BlueprintResponse.from_dict(decode_message(encode_message(response))).validate_for(request)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_endpoint_id", "22222222-2222-4222-8222-222222222222"),
        ("pipeline_id", "different"),
        ("deal_stage_ids", ["different"]),
        ("interval_minutes", 10),
        ("invoice_due_days", 90),
        ("updated_since", "2025-01-01"),
        ("mapping", {"id": "another", "revision": "8", "digest": "sha256:" + "d" * 64}),
    ],
)
def test_generator_cannot_change_selections_by_echoing_request_metadata(field, value):
    from sanka_extensions.flow import BlueprintResponse

    request, candidate = request_and_result()
    payload = candidate.to_dict()
    payload["resources"][0]["spec"][field] = value
    changed = Blueprint.from_dict(payload)
    with pytest.raises(ValueError, match="executable configuration"):
        BlueprintResponse.success(request, changed)

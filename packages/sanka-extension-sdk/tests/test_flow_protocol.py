# SPDX-License-Identifier: Apache-2.0
"""Isolated generator messages preserve the caller's exact reviewed choices."""

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from sanka_extensions import flow


@pytest.fixture
def flow_request():
    payload = json.loads(
        (Path(__file__).parent / "fixtures" / "synthetic_sales_quote_blueprint.json").read_text()
    )
    blueprint = flow.Blueprint.from_dict(payload)
    values = {"quote_stage": "Quote", "other_stage": "Negotiation", "draft_status": "draft"}
    return flow.BlueprintRequest(
        "generate-1",
        blueprint.extension,
        flow.create(type="example/sales-quote", parameters={"language": "ja"}),
        flow.TargetIdentity("synthetic-workspace", "revision-1"),
        blueprint.references,
        values,
    )


@pytest.fixture
def blueprint(flow_request):
    payload = json.loads(
        (Path(__file__).parent / "fixtures" / "synthetic_sales_quote_blueprint.json").read_text()
    )
    payload["parameters"] = flow_request.blueprint_parameters
    return flow.Blueprint.from_dict(payload)


@pytest.fixture
def capability(flow_request):
    return flow.FlowCapability(
        flow_request.definition.type,
        tuple(
            flow.ReferenceRequirement(r.id, r.kind, r.parent_id, r.related_object_id)
            for r in flow_request.references
        ),
        tuple(flow.ValueRequirement(name, ("string",)) for name in flow_request.values),
    )


def test_typed_protocol_round_trip_preserves_input_and_capability(
    flow_request, blueprint, capability
):
    capability.validate_request(flow_request)
    assert (
        flow.BlueprintRequest.from_dict(flow.decode_message(flow.encode_message(flow_request)))
        == flow_request
    )
    response = flow.BlueprintResponse.success(flow_request, blueprint)
    decoded = flow.BlueprintResponse.from_dict(flow.decode_message(flow.encode_message(response)))
    assert decoded == response
    decoded.validate_for(flow_request)
    assert decoded.request_digest == flow_request.digest
    assert flow.FlowCapability.from_dict(capability.to_dict()) == capability


def test_requests_own_input_and_output_values(flow_request):
    payload = flow_request.to_dict()
    original = deepcopy(payload)
    restored = flow.BlueprintRequest.from_dict(payload)
    payload["values"]["quote_stage"] = "Closed"
    payload["definition"]["parameters"]["language"] = "en"
    restored.values.clear()
    restored.blueprint_parameters["target"]["id"] = "another-workspace"
    assert restored.to_dict() == original


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "request_id",
        "operation",
        "extension",
        "definition",
        "target",
        "references",
        "values",
    ],
)
def test_request_requires_exact_fields(flow_request, field):
    payload = flow_request.to_dict()
    del payload[field]
    with pytest.raises(ValueError, match="unexpected or missing"):
        flow.BlueprintRequest.from_dict(payload)


@pytest.mark.parametrize("operation", ["activate", "apply", "source", "scan"])
def test_template_protocol_cannot_be_used_for_source_or_mutating_operations(
    flow_request, operation
):
    payload = flow_request.to_dict()
    payload["operation"] = operation
    with pytest.raises(ValueError, match="Flow operation"):
        flow.BlueprintRequest.from_dict(payload)


def test_source_snapshot_input_is_not_a_template_definition(flow_request):
    payload = flow_request.to_dict()
    payload["definition"] = {"schema_version": "sanka-flow-source/v1", "nodes": []}
    with pytest.raises(ValueError):
        flow.BlueprintRequest.from_dict(payload)
    payload = flow_request.to_dict()
    payload["source"] = {"nodes": []}
    with pytest.raises(ValueError, match="unexpected or missing"):
        flow.BlueprintRequest.from_dict(payload)


@pytest.mark.parametrize("value", [["Quote"], {"value": "Quote"}, float("inf"), float("nan")])
def test_selected_values_must_be_finite_json_scalars(flow_request, value):
    payload = flow_request.to_dict()
    payload["values"]["quote_stage"] = value
    with pytest.raises(ValueError):
        flow.BlueprintRequest.from_dict(payload)


def test_request_cannot_pass_source_or_planned_resource_references(flow_request):
    payload = flow_request.to_dict()
    for reference in payload["references"]:
        reference["scope"] = "source"
    with pytest.raises(ValueError, match="existing target"):
        flow.BlueprintRequest.from_dict(payload)
    payload = flow_request.to_dict()
    payload["references"][0]["binding"] = "resource"
    with pytest.raises(ValueError, match="existing target"):
        flow.BlueprintRequest.from_dict(payload)


@pytest.mark.parametrize("change", ["request_id", "request_digest", "extension"])
def test_response_correlation_cannot_be_spoofed(flow_request, blueprint, change):
    response = flow.BlueprintResponse.success(flow_request, blueprint)
    payload = response.to_dict()
    if change == "extension":
        payload["extension"]["digest"] = "sha256:" + "0" * 64
        payload["blueprint"]["extension"] = deepcopy(payload["extension"])
    else:
        payload[change] = "other-request" if change == "request_id" else "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="different requested input"):
        flow.BlueprintResponse.from_dict(payload).validate_for(flow_request)


@pytest.mark.parametrize(
    "change", ["target", "values", "definition_parameters", "references", "origin"]
)
def test_output_cannot_silently_change_selected_meaning(flow_request, blueprint, change):
    payload = blueprint.to_dict()
    if change == "references":
        payload["references"][0]["key"] = "another-object"
    elif change == "origin":
        payload["origin"]["identity"]["id"] = "other/template"
    else:
        payload["parameters"][change] = {"changed": True}
    with pytest.raises(ValueError, match="Flow output"):
        flow.BlueprintResponse.success(flow_request, flow.Blueprint.from_dict(payload))


def test_handled_errors_have_no_blueprint_and_are_correlated(flow_request):
    response = flow.BlueprintResponse.failure(
        flow_request, code="FLOW_INPUT_INVALID", message="Unsupported selected field"
    )
    response.validate_for(flow_request)
    assert response.outcome == "error" and response.blueprint is None
    assert flow.BlueprintResponse.from_dict(response.to_dict()) == response
    with pytest.raises(ValueError, match="success requires"):
        replace(response, outcome="success")


def test_capability_requires_exact_reference_roles_and_value_types(flow_request, capability):
    payload = flow_request.to_dict()
    payload["references"].append(flow.Reference("extra", "object", "extra-object").to_dict())
    with pytest.raises(ValueError, match="exactly the declared reference roles"):
        capability.validate_request(flow.BlueprintRequest.from_dict(payload))
    payload = flow_request.to_dict()
    payload["values"]["quote_stage"] = 1
    with pytest.raises(ValueError, match="wrong type"):
        capability.validate_request(flow.BlueprintRequest.from_dict(payload))
    payload = flow_request.to_dict()
    del payload["values"]["other_stage"]
    with pytest.raises(ValueError, match="exactly the declared selected values"):
        capability.validate_request(flow.BlueprintRequest.from_dict(payload))


def test_manifest_requirements_use_present_nullable_fields(capability):
    payload = capability.to_dict()
    assert all(
        set(ref) == {"id", "kind", "parent_id", "related_object_id"}
        for ref in payload["references"]
    )
    del payload["references"][0]["related_object_id"]
    with pytest.raises(ValueError, match="unexpected or missing"):
        flow.FlowCapability.from_dict(payload)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"a":1,"a":2}',
        b'{"nested":{"a":1,"a":2}}',
        b"{}{}",
        b"[]",
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":"\xff"}',
        b'{"value":"\\ud800"}',
        b'{"unknown":' + b"[" * 1200 + b"0" + b"]" * 1200 + b"}",
    ],
)
def test_message_parser_rejects_ambiguous_or_invalid_json(payload):
    with pytest.raises(ValueError):
        flow.decode_message(payload)


def test_message_byte_limits_apply_before_decode_and_after_encode(flow_request):
    with pytest.raises(ValueError, match="byte limit"):
        flow.decode_message(b" " * (flow.MAX_MESSAGE_BYTES + 1))
    payload = flow_request.to_dict()
    payload["definition"]["parameters"] = {"too_large": "x" * flow.MAX_MESSAGE_BYTES}
    with pytest.raises(ValueError, match="byte limit"):
        flow.encode_message(flow.BlueprintRequest.from_dict(payload))


def test_unicode_protocol_and_request_digest_are_exact_utf8(flow_request):
    payload = flow_request.to_dict()
    payload["values"]["quote_stage"] = "見積"
    decoded = flow.BlueprintRequest.from_dict(payload)
    message = flow.encode_message(decoded)
    assert "見積".encode() in message
    assert flow.BlueprintRequest.from_dict(flow.decode_message(message)).digest == decoded.digest

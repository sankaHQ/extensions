# SPDX-License-Identifier: Apache-2.0
"""Native verification must pin fixtures and distinguish retries from schedules."""

from copy import deepcopy
from dataclasses import replace

import pytest
from test_flow_native import blueprint, workflow

from sanka_extensions.flow import ArtifactIdentity, Blueprint
from sanka_extensions.flow.native_verification import (
    NATIVE_BILLING_CASES,
    NativeBillingDelivery,
    NativeBillingScenario,
    validate_billing_scenarios,
)


def scenario(case="complete"):
    keys = ("deal-a", "deal-b", "deal-c")
    if case == "bulk":
        keys = tuple(f"deal-{i:04d}" for i in range(2001))
    existing = (keys[0],)
    status = case if case in {"partial", "failed", "cancelled"} else "complete"
    imported = () if case == "empty" else keys
    if case == "scoped_complete":
        imported = (keys[0], keys[2])
    billed = existing if status != "complete" or case == "empty" else keys
    if case == "scoped_complete":
        billed = imported
    first = NativeBillingDelivery(
        "attempt-1", "run-1", "2026-09-14T00:00:00Z", status, imported, billed
    )
    deliveries = (first,)
    if case == "retry":
        first = replace(first, failure="after_invoice_commit", invoice_keys=(*existing, keys[1]))
        deliveries = (first, replace(first, id="attempt-2", failure="none", invoice_keys=keys))
    if case == "handoff_retry":
        first = replace(first, failure="after_import", invoice_keys=existing)
        deliveries = (first, replace(first, id="attempt-2", failure="none", invoice_keys=keys))
    if case == "repeated_schedule":
        deliveries = (
            first,
            replace(first, id="attempt-2", run_id="run-2", at="2026-09-14T00:30:00Z"),
        )
    if case == "overlap":
        first = replace(first, concurrent_group="race-1")
        deliveries = (first, replace(first, id="attempt-2", run_id="run-2"))
    return NativeBillingScenario(
        id=case,
        workflow_id="billing",
        case=case,
        fixture=ArtifactIdentity("isolated-fixture/" + case, "1", "sha256:" + "d" * 64),
        mapping=workflow().mapping,
        configuration_digest=workflow().configuration_digest,
        record_keys=keys,
        existing_order_keys=(keys[0], keys[1]),
        existing_invoice_keys=existing,
        deliveries=deliveries,
    )


def all_scenarios():
    return tuple(scenario(case) for case in NATIVE_BILLING_CASES)


def verified_blueprint():
    original = blueprint()
    return Blueprint(
        id=original.id,
        revision=original.revision,
        origin=original.origin,
        extension=original.extension,
        resources=original.resources,
        scenarios=all_scenarios(),
        schema_version="sanka-flow-blueprint/v4",
    )


def test_v4_round_trip_requires_all_native_cases_and_verifier_capability():
    candidate = verified_blueprint()
    assert Blueprint.from_dict(candidate.to_dict()).digest == candidate.digest
    assert "flow.native.order-billing-verification/v1" in candidate.required_capabilities
    with pytest.raises(ValueError, match="capabilities"):
        candidate.require_supported(capabilities=frozenset(blueprint().required_capabilities))
    validate_billing_scenarios(workflow(), all_scenarios())


@pytest.mark.parametrize("case", NATIVE_BILLING_CASES)
def test_every_case_is_required(case):
    with pytest.raises(ValueError, match="coverage"):
        validate_billing_scenarios(workflow(), tuple(s for s in all_scenarios() if s.case != case))


def test_rejects_stale_mapping_or_executable_configuration():
    for changed in (
        replace(scenario(), mapping=ArtifactIdentity("other", "1", "sha256:" + "e" * 64)),
        replace(scenario(), configuration_digest="sha256:" + "f" * 64),
    ):
        candidates = tuple(changed if s.case == "complete" else s for s in all_scenarios())
        with pytest.raises(ValueError, match="configuration or mapping"):
            validate_billing_scenarios(workflow(), candidates)


@pytest.mark.parametrize("case", ["partial", "failed", "cancelled"])
def test_incomplete_imports_cannot_expect_new_invoices(case):
    original = scenario(case)
    with pytest.raises(ValueError, match="incomplete"):
        replace(
            original,
            deliveries=(replace(original.deliveries[0], invoice_keys=original.record_keys),),
        )


@pytest.mark.parametrize("case", ["partial", "failed", "cancelled"])
def test_incomplete_negative_control_cannot_hide_behind_an_already_billed_order(case):
    original = scenario(case)
    with pytest.raises(ValueError, match="existing unbilled"):
        replace(
            original,
            deliveries=(
                replace(original.deliveries[0], imported_keys=original.existing_invoice_keys),
            ),
        )


def test_nonempty_import_must_exclude_an_existing_unbilled_order_in_scope_control():
    original = scenario("scoped_complete")
    with pytest.raises(ValueError, match="outside a nonempty import"):
        replace(
            original,
            deliveries=(
                replace(
                    original.deliveries[0],
                    imported_keys=original.record_keys,
                    invoice_keys=original.record_keys,
                ),
            ),
        )


def test_retry_and_repeated_schedule_are_different_native_operations():
    retry = scenario("retry")
    with pytest.raises(ValueError, match="same run"):
        replace(retry, deliveries=(retry.deliveries[0], replace(retry.deliveries[1], run_id="new")))
    repeated = scenario("repeated_schedule")
    with pytest.raises(ValueError, match="distinct runs"):
        replace(
            repeated,
            deliveries=(repeated.deliveries[0], replace(repeated.deliveries[1], run_id="run-1")),
        )


def test_overlap_cannot_be_sequential_deliveries_or_a_single_run():
    original = scenario("overlap")
    with pytest.raises(ValueError, match="concurrent"):
        replace(
            original,
            deliveries=tuple(replace(d, concurrent_group=None) for d in original.deliveries),
        )
    with pytest.raises(ValueError, match="distinct runs"):
        replace(original, deliveries=tuple(replace(d, run_id="same") for d in original.deliveries))


def test_v3_cannot_claim_native_verification_or_change_its_wire_digest():
    original = blueprint()
    payload = original.to_dict()
    assert Blueprint.from_dict(payload).digest == original.digest
    payload["scenarios"] = [scenario().to_dict()]
    with pytest.raises(ValueError):
        Blueprint.from_dict(payload)
    changed = deepcopy(verified_blueprint().to_dict())
    changed["required_capabilities"].remove("flow.native.order-billing-verification/v1")
    with pytest.raises(ValueError, match="required_capabilities"):
        Blueprint.from_dict(changed)


def test_deliveries_reject_unknown_fields_and_noncanonical_clock():
    payload = scenario().deliveries[0].to_dict()
    payload["execute"] = "arbitrary script"
    with pytest.raises(ValueError, match="unexpected"):
        NativeBillingDelivery.from_dict(payload)
    with pytest.raises(ValueError, match="UTC"):
        replace(scenario().deliveries[0], at="2026-09-14T00:00:00+09:00")


def native_request():
    from sanka_extensions.flow import BlueprintRequest, FlowDefinition, TargetIdentity

    candidate = verified_blueprint()
    return BlueprintRequest(
        "native-v4-request",
        candidate.extension,
        FlowDefinition(
            type=candidate.origin.identity.id,
            parameters={
                "native_configuration": workflow().configuration,
                "native_verification": [s.to_dict() for s in candidate.scenarios],
            },
        ),
        TargetIdentity("isolated-workspace", "revision-1", candidate.required_capabilities),
        (),
        {},
        template=candidate.origin.identity,
        output_schema="sanka-flow-blueprint/v4",
    )


def test_generator_must_preserve_admitted_fixture_oracle_and_complete_coverage():
    from sanka_extensions.flow import BlueprintResponse

    request = native_request()
    payload = verified_blueprint().to_dict()
    payload["parameters"] = request.blueprint_parameters
    original = Blueprint.from_dict(payload)
    BlueprintResponse.success(request, original)
    payload["scenarios"][0]["fixture"]["digest"] = "sha256:" + "e" * 64
    with pytest.raises(ValueError, match="admitted verification fixtures"):
        BlueprintResponse.success(request, Blueprint.from_dict(payload))


def test_2001_case_round_trips_through_complete_generator_messages_without_inline_fixtures():
    from sanka_extensions.flow import (
        BlueprintRequest,
        BlueprintResponse,
        decode_message,
        encode_message,
    )

    request = native_request()
    encoded = encode_message(request)
    restored = BlueprintRequest.from_dict(decode_message(encoded))
    payload = verified_blueprint().to_dict()
    payload["parameters"] = restored.blueprint_parameters
    response = BlueprintResponse.success(restored, Blueprint.from_dict(payload))
    encoded_response = encode_message(response)
    BlueprintResponse.from_dict(decode_message(encoded_response)).validate_for(restored)
    assert len(encoded) < 256 * 1024
    assert len(encoded_response) < 512 * 1024

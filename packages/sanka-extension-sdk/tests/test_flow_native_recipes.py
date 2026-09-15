# SPDX-License-Identifier: Apache-2.0
"""Recipe families retain distinct semantics without arbitrary native payloads."""

from copy import deepcopy

import pytest

from sanka_extensions.flow import (
    ArtifactIdentity,
    Blueprint,
    BlueprintOrigin,
    BlueprintRequest,
    BlueprintResponse,
    FlowDefinition,
    NativeAssignedTaskWorkflow,
    NativeBusinessProcessWorkflow,
    NativeRecordConversionWorkflow,
    NativeSourceApprovalWorkflow,
    Resource,
    TargetIdentity,
)

CONVERSIONS = [
    ("deal_to_estimate", {}),
    ("won_deal_to_order", {"won_stage": "受注済み"}),
    ("order_to_invoice", {"invoice_notes": None}),
    ("purchase_order_to_bill", {"use_po_date": True, "bill_due_days": 30, "bill_notes": ""}),
    (
        "order_to_stock_out",
        {
            "rotate_inventory": True,
            "subtract_from_components": False,
            "require_all_components_in_stock": True,
        },
    ),
]


@pytest.mark.parametrize("schema", [[], {}])
def test_malformed_profile_discriminator_uses_wire_validation_error(schema):
    with pytest.raises(ValueError):
        Resource("workflow", "workflow", {"schema_version": schema})


def request_for(profile):
    template = ArtifactIdentity("sanka/native-recipe", "1", "sha256:" + "a" * 64)
    return BlueprintRequest(
        request_id="test",
        extension=ArtifactIdentity("sanka/business-flows", "2", "sha256:" + "b" * 64),
        definition=FlowDefinition(
            type=template.id, parameters={"native_workflow": profile.to_dict()}
        ),
        target=TargetIdentity(
            "workspace",
            "1",
            tuple(
                sorted(
                    {
                        "sanka-flow-blueprint/v5",
                        "flow.resource.workflow/v1",
                        *profile.required_capabilities,
                    }
                )
            ),
        ),
        references=(),
        values={},
        template=template,
        output_schema="sanka-flow-blueprint/v5",
    )


def blueprint_for(profile, request):
    return Blueprint(
        id=profile.id,
        revision="1",
        origin=BlueprintOrigin("template", request.template),
        extension=request.extension,
        resources=(Resource(profile.id, "workflow", profile.to_dict()),),
        parameters=request.blueprint_parameters,
        schema_version="sanka-flow-blueprint/v5",
    )


@pytest.mark.parametrize("conversion,settings", CONVERSIONS)
def test_conversion_round_trip_capabilities_and_immutable_settings(conversion, settings):
    supplied = deepcopy(settings)
    profile = NativeRecordConversionWorkflow("workflow", conversion, supplied)
    before = profile.to_dict()
    supplied["unrecognized"] = "not executable"
    profile.configuration["settings"]["unrecognized"] = "not mutable"
    assert profile.to_dict() == before
    assert NativeRecordConversionWorkflow.from_dict(before).to_dict() == before
    request = request_for(profile)
    candidate = blueprint_for(profile, request)
    BlueprintResponse.success(request, candidate).validate_for(request)
    assert Blueprint.from_dict(candidate.to_dict()).digest == candidate.digest
    with pytest.raises(ValueError, match="capabilities"):
        candidate.require_supported(capabilities=frozenset({"flow.resource.workflow/v1"}))


def test_conversion_policies_do_not_claim_the_same_side_effects_or_retry_behavior():
    profiles = {
        kind: NativeRecordConversionWorkflow("workflow", kind, settings)
        for kind, settings in CONVERSIONS
    }
    assert profiles["deal_to_estimate"].policies["amount_and_lines"] == "not_copied"
    assert profiles["won_deal_to_order"].policies["existing_bound_order"] == "preserve"
    assert profiles["order_to_invoice"].policies["billing_options"] == "native_conversion_defaults"
    assert "existing_linked_bill" not in profiles["order_to_invoice"].policies
    assert profiles["purchase_order_to_bill"].policies["existing_linked_bill"] == "skip"
    assert profiles["order_to_stock_out"].policies["quantities"] == "order_lines"
    assert len({profile.required_capabilities for profile in profiles.values()}) == 5


@pytest.mark.parametrize("subject", ["purchase_order", "expense", "absence"])
def test_approval_sequence_is_not_sorted_and_never_changes_source_status(subject):
    profile = NativeSourceApprovalWorkflow("approval", subject, ("502", "501"))
    reordered = NativeSourceApprovalWorkflow("approval", subject, ("501", "502"))
    assert profile.configuration_digest != reordered.configuration_digest
    assert profile.node_ids == reordered.node_ids
    assert profile.to_dict()["approver_ids"] == ["502", "501"]
    assert profile.policies["source_status"] == "unchanged"
    assert profile.policies["decision_target"] == "workflow_history"
    request = request_for(profile)
    candidate = blueprint_for(profile, request)
    BlueprintResponse.success(request, candidate).validate_for(request)
    assert NativeSourceApprovalWorkflow.from_dict(profile.to_dict()) == profile


@pytest.mark.parametrize(
    "ids",
    [
        (),
        ("1", "1"),
        ("alice",),
        ("0",),
        ("01",),
        ("-1",),
        (str(2**63),),
        ("\uff11\uff12",),
        tuple(str(i) for i in range(1, 102)),
    ],
)
def test_approval_requires_bounded_canonical_workspace_user_ids(ids):
    with pytest.raises(ValueError):
        NativeSourceApprovalWorkflow("approval", "expense", ids)


@pytest.mark.parametrize(
    "kind,settings",
    [
        ("any_handler", {}),
        ("deal_to_estimate", {"copy_all_fields": True}),
        ("won_deal_to_order", {"won_stage": " "}),
        ("order_to_invoice", {"invoice_notes": "x" * 2001}),
        ("order_to_invoice", {"invoice_notes": None, "skip_already_invoiced": True}),
        ("purchase_order_to_bill", {"use_po_date": 1, "bill_due_days": 30, "bill_notes": ""}),
        ("purchase_order_to_bill", {"use_po_date": True, "bill_due_days": True, "bill_notes": ""}),
        ("purchase_order_to_bill", {"use_po_date": True, "bill_due_days": 366, "bill_notes": ""}),
        ("order_to_stock_out", {"rotate_inventory": True, "subtract_from_components": False}),
    ],
)
def test_no_untyped_settings_or_cross_recipe_options(kind, settings):
    with pytest.raises(ValueError):
        NativeRecordConversionWorkflow("workflow", kind, settings)


def test_response_cannot_change_approval_order_or_stable_node_identity():
    profile = NativeSourceApprovalWorkflow("approval", "expense", ("502", "501"))
    request = request_for(profile)
    for changed in (
        NativeSourceApprovalWorkflow("approval", "expense", ("501", "502")),
        NativeSourceApprovalWorkflow("replacement", "expense", ("502", "501")),
    ):
        with pytest.raises(ValueError, match="executable workflow"):
            BlueprintResponse.success(request, blueprint_for(changed, request))


@pytest.mark.parametrize(
    "profile",
    [
        NativeRecordConversionWorkflow("convert", "deal_to_estimate", {}),
        NativeSourceApprovalWorkflow("approve", "absence", ("501",)),
    ],
)
def test_fixed_native_policies_and_legacy_blueprint_boundaries(profile):
    payload = profile.to_dict()
    payload["policies"]["construction"] = "active"
    with pytest.raises(ValueError, match="policies"):
        type(profile).from_dict(payload)
    payload = profile.to_dict()
    payload["node_ids"].reverse()
    with pytest.raises(ValueError, match="identities"):
        type(profile).from_dict(payload)
    request = request_for(profile)
    candidate = blueprint_for(profile, request).to_dict()
    for version in (
        "sanka-flow-blueprint/v1",
        "sanka-flow-blueprint/v2",
        "sanka-flow-blueprint/v3",
        "sanka-flow-blueprint/v4",
    ):
        candidate["schema_version"] = version
        with pytest.raises(ValueError):
            Blueprint.from_dict(candidate)


TASK_SETTINGS = {
    "stalled_deal_follow_up": {
        "local_hour": 9,
        "open_stages": ["76c363a0-82f4-412a-bb57-a2fca72ec778"],
        "stale_days": 7,
    },
    "overdue_invoice_reminder": {"local_hour": 9, "overdue_days": 1},
    "supplier_payment_reminder": {"local_hour": 9, "days_before_due": 7},
    "low_stock_replenishment": {"local_hour": 9, "stock_threshold": 10},
    "stock_discrepancy_review": {"local_hour": 9, "difference_tolerance": 0},
    "expense_reimbursement_preparation": {"local_hour": 9},
    "expense_accounting_review": {},
    "missing_expense_receipt": {"local_hour": 9, "wait_days": 3},
    "project_task_checklist": {"task_titles": ["Prepare", "Review"]},
    "milestone_reminder": {
        "task_names": ["Review"],
        "open_statuses": ["todo", "in_progress"],
        "days_before_due": 7,
    },
    "overdue_task_escalation": {"open_statuses": ["todo", "in_progress"], "overdue_days": 1},
    "unresolved_ticket_escalation": {"stale_days": 7},
    "ticket_resolution_follow_up": {"wait_days": 3},
    "employee_onboarding": {
        "task_titles": ["Prepare laptop", "Meet team"],
        "days_before_start": 7,
        "catch_up_days": 7,
    },
    "missing_attendance": {
        "employee_ids": ["90"],
        "weekdays": ["mon", "fri"],
        "excluded_dates": ["2028-02-29"],
        "lookback_days": 7,
    },
}
PROCESS_SETTINGS = {
    "monthly_invoice_consolidation": {"local_hour": 9, "billing_day": 1, "invoice_due_days": 30},
    "ticket_assignment": {"assignee_ids": ["502", "501"]},
    "sales_delivery_billing": {
        "won_stage": "won",
        "assignee_ids": ["501"],
        "due_days": 7,
        "invoice_due_days": 30,
    },
    "replenishment_purchasing": {
        "inventory_id": "76c363a0-82f4-412a-bb57-a2fca72ec778",
        "supplier_id": "faef8fef-c30b-4431-9c63-c6910028c6c1",
        "stock_threshold": 10,
        "reorder_quantity": 25,
        "unit_price": "120.50",
        "currency": "JPY",
        "tax_rate": 10,
        "assignee_ids": ["501"],
        "due_days": 7,
    },
}


def task_settings(task):
    return {"assignee_ids": ["502", "501"], "due_days": 7, **deepcopy(TASK_SETTINGS[task])}


@pytest.mark.parametrize("task", TASK_SETTINGS)
def test_task_request_round_trip_preserves_canonical_selections_and_provenance(task):
    settings = task_settings(task)
    profile = NativeAssignedTaskWorkflow("task", task, settings)
    request = request_for(profile)
    candidate = blueprint_for(profile, request)
    assert Blueprint.from_dict(candidate.to_dict()).digest == candidate.digest
    BlueprintResponse.success(request, candidate).validate_for(request)
    settings["assignee_ids"].reverse()
    assert profile.configuration["settings"]["assignee_ids"] == ["502", "501"]
    assert profile.policies["result"] == "assigned_tasks"
    assert profile.policies["source_status"] == "unchanged"
    assert profile.policies["external_delivery"] == "none"
    modified = deepcopy(profile.configuration["settings"])
    modified["due_days"] = 8
    changed = NativeAssignedTaskWorkflow("task", task, modified)
    assert changed.node_ids == profile.node_ids
    assert changed.configuration_digest != profile.configuration_digest
    with pytest.raises(ValueError, match="executable workflow"):
        BlueprintResponse.success(request, blueprint_for(changed, request))


@pytest.mark.parametrize("process,settings", PROCESS_SETTINGS.items())
def test_business_process_round_trip_capability_and_response_binding(process, settings):
    profile = NativeBusinessProcessWorkflow("process", process, settings)
    request = request_for(profile)
    candidate = blueprint_for(profile, request)
    BlueprintResponse.success(request, candidate).validate_for(request)
    assert Blueprint.from_dict(candidate.to_dict()).digest == candidate.digest
    assert NativeBusinessProcessWorkflow.from_dict(profile.to_dict()).to_dict() == profile.to_dict()
    with pytest.raises(ValueError, match="capabilities"):
        candidate.require_supported(
            capabilities=frozenset({"sanka-flow-blueprint/v5", "flow.resource.workflow/v1"})
        )


@pytest.mark.parametrize(
    "task,key,value",
    [
        ("stalled_deal_follow_up", "open_stages", ["won"]),
        ("stalled_deal_follow_up", "open_stages", ["76C363A0-82F4-412A-BB57-A2FCA72EC778"]),
        ("stalled_deal_follow_up", "stale_days", 0),
        ("overdue_invoice_reminder", "overdue_days", True),
        ("overdue_invoice_reminder", "local_hour", 24),
        ("supplier_payment_reminder", "days_before_due", -1),
        ("low_stock_replenishment", "stock_threshold", 1_000_001),
        ("stock_discrepancy_review", "difference_tolerance", 0.1),
        ("missing_expense_receipt", "wait_days", 0),
        ("project_task_checklist", "task_titles", ["same", "same"]),
        ("project_task_checklist", "task_titles", [" review "]),
        ("project_task_checklist", "task_titles", ["x" * 201]),
        ("project_task_checklist", "task_titles", [str(i) for i in range(21)]),
        ("milestone_reminder", "task_names", []),
        ("milestone_reminder", "open_statuses", ["todo", "todo"]),
        ("milestone_reminder", "open_statuses", [str(i) for i in range(51)]),
        ("employee_onboarding", "days_before_start", 366),
        ("employee_onboarding", "catch_up_days", -1),
        ("missing_attendance", "employee_ids", ["090"]),
        ("missing_attendance", "weekdays", ["Mon"]),
        ("missing_attendance", "weekdays", ["mon", "mon"]),
        ("missing_attendance", "excluded_dates", ["2027-02-29"]),
        ("missing_attendance", "excluded_dates", ["20280229"]),
        ("missing_attendance", "lookback_days", 32),
    ],
)
def test_task_rejects_invalid_source_and_calendar_configuration(task, key, value):
    settings = task_settings(task)
    settings[key] = value
    with pytest.raises(ValueError):
        NativeAssignedTaskWorkflow("task", task, settings)


@pytest.mark.parametrize("task", TASK_SETTINGS)
def test_task_settings_cannot_inject_handlers_or_other_recipe_options(task):
    settings = task_settings(task)
    settings["domain_handler"] = {"key": "arbitrary"}
    with pytest.raises(ValueError):
        NativeAssignedTaskWorkflow("task", task, settings)
    settings = task_settings(task)
    settings["assignee_ids"] = ["501", "0501"]
    with pytest.raises(ValueError):
        NativeAssignedTaskWorkflow("task", task, settings)
    del settings["due_days"]
    with pytest.raises(ValueError):
        NativeAssignedTaskWorkflow("task", task, settings)


def test_task_recipes_preserve_distinct_schedules_and_batch_semantics():
    checklist = NativeAssignedTaskWorkflow(
        "x", "project_task_checklist", task_settings("project_task_checklist")
    )
    onboarding = NativeAssignedTaskWorkflow(
        "x", "employee_onboarding", task_settings("employee_onboarding")
    )
    overdue = NativeAssignedTaskWorkflow(
        "x", "overdue_invoice_reminder", task_settings("overdue_invoice_reminder")
    )
    attendance = NativeAssignedTaskWorkflow(
        "x", "missing_attendance", task_settings("missing_attendance")
    )
    assert checklist.policies["trigger"] == "project.created"
    assert "batch_limit" not in checklist.policies
    assert onboarding.policies["batch_unit"] == "employees"
    assert overdue.policies["batch_unit"] == "tasks"
    assert attendance.policies["visibility"] == "complete_attendance_and_absence_read_scope"
    assert attendance.policies["source_revision"] == "completed_local_workday"
    assert "existing_open_source_task" not in overdue.policies
    assert onboarding.policies["local_hour"] == 9
    with pytest.raises(ValueError):
        NativeAssignedTaskWorkflow(
            "x", "employee_onboarding", {**task_settings("employee_onboarding"), "local_hour": 10}
        )
    mutable = task_settings("overdue_invoice_reminder")
    mutable["local_hour"] = 23
    selected = NativeAssignedTaskWorkflow("x", "overdue_invoice_reminder", mutable)
    assert selected.policies["local_hour"] == 23
    assert selected.policies["schedule_resolution"] == "workspace_offset_at_construction"


@pytest.mark.parametrize(
    "process,key,value",
    [
        ("monthly_invoice_consolidation", "billing_day", 29),
        ("monthly_invoice_consolidation", "invoice_due_days", -1),
        ("monthly_invoice_consolidation", "local_hour", False),
        ("ticket_assignment", "assignee_ids", []),
        ("ticket_assignment", "assignee_ids", ["501", "501"]),
        ("sales_delivery_billing", "won_stage", "x" * 201),
        ("sales_delivery_billing", "won_stage", " "),
        ("sales_delivery_billing", "due_days", 0),
        ("replenishment_purchasing", "inventory_id", "not-a-record"),
        ("replenishment_purchasing", "supplier_id", None),
        ("replenishment_purchasing", "stock_threshold", -1),
        ("replenishment_purchasing", "reorder_quantity", 0),
        ("replenishment_purchasing", "currency", "ALL"),
        ("replenishment_purchasing", "tax_rate", 101),
        ("replenishment_purchasing", "unit_price", 1.25),
        ("replenishment_purchasing", "unit_price", "NaN"),
        ("replenishment_purchasing", "unit_price", "Infinity"),
        ("replenishment_purchasing", "unit_price", "-1"),
        ("replenishment_purchasing", "unit_price", "1000000001"),
        ("replenishment_purchasing", "unit_price", "0.0000001"),
        ("replenishment_purchasing", "unit_price", "1e2"),
        ("replenishment_purchasing", "unit_price", "+1.0"),
        ("replenishment_purchasing", "unit_price", "0e1000000000"),
        ("replenishment_purchasing", "unit_price", "-0e1000000000"),
    ],
)
def test_process_rejects_invalid_settings_without_numeric_coercion(process, key, value):
    settings = deepcopy(PROCESS_SETTINGS[process])
    settings[key] = value
    with pytest.raises(ValueError):
        NativeBusinessProcessWorkflow("process", process, settings)


def test_process_policies_do_not_conflate_payment_delivery_or_batch_atomicity():
    profiles = {
        kind: NativeBusinessProcessWorkflow("x", kind, values)
        for kind, values in PROCESS_SETTINGS.items()
    }
    monthly = profiles["monthly_invoice_consolidation"].policies
    assert monthly["selection_limit"] == 5000
    assert monthly["selection_overflow"] == "fail_before_any_write"
    assert (
        monthly["partial_failure"] == "retain_completed_groups_and_retry_remaining_unbilled_orders"
    )
    assert profiles["sales_delivery_billing"].policies["delivery_completion_required"] is False
    assert (
        profiles["replenishment_purchasing"].policies["outstanding_purchase"]
        == "skip_until_in_stock_or_archived"
    )
    assert profiles["ticket_assignment"].policies["existing_owner"] == "preserve"
    assert profiles["ticket_assignment"].policies["pool_order"] == "ascending_workspace_user_id"
    for profile in profiles.values():
        payload = profile.to_dict()
        payload["policies"]["construction"] = "active"
        with pytest.raises(ValueError, match="policies"):
            NativeBusinessProcessWorkflow.from_dict(payload)
    payload = profiles["sales_delivery_billing"].to_dict()
    payload["policies"]["delivery_completion_required"] = 0
    with pytest.raises(ValueError, match="policies"):
        NativeBusinessProcessWorkflow.from_dict(payload)


@pytest.mark.parametrize("kind", ["task", "process"])
def test_new_families_do_not_enter_legacy_blueprints(kind):
    profile = (
        NativeAssignedTaskWorkflow("x", "missing_attendance", task_settings("missing_attendance"))
        if kind == "task"
        else NativeBusinessProcessWorkflow(
            "x", "ticket_assignment", PROCESS_SETTINGS["ticket_assignment"]
        )
    )
    request = request_for(profile)
    payload = blueprint_for(profile, request).to_dict()
    for version in range(1, 5):
        payload["schema_version"] = f"sanka-flow-blueprint/v{version}"
        with pytest.raises(ValueError):
            Blueprint.from_dict(payload)

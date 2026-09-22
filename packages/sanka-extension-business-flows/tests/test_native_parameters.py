# SPDX-License-Identifier: Apache-2.0
"""Native normalization parity and rejected configuration boundaries."""

import copy

import pytest

from sanka_extension_business_flows import resolve_parameters
from sanka_extension_business_flows.native_generator import native_profile


def settings(recipe_id, supplied):
    configuration = native_profile(recipe_id, supplied).configuration
    return configuration.get("settings", configuration)


def test_task_id_aliases_deduplicate_without_reordering_or_mutating_input():
    supplied = {"assignee_ids": ["0502", "502", "501"]}
    assert settings("billing.overdue-reminders", supplied)["assignee_ids"] == ["502", "501"]
    assert resolve_parameters("billing.overdue-reminders", supplied)["assignee_ids"] == [
        "0502",
        "502",
        "501",
    ]
    assert supplied == {"assignee_ids": ["0502", "502", "501"]}


@pytest.mark.parametrize("value", [" 502", "\uff15\uff10\uff12", "0", "-1", "+1", str(2**63)])
def test_task_ids_reject_invalid_native_workspace_member_spelling(value):
    with pytest.raises(ValueError):
        settings("billing.overdue-reminders", {"assignee_ids": [value]})


def test_approval_numeric_aliases_are_rejected_after_catalog_exact_duplicate_handling():
    recipe_id = "purchasing.purchase-approvals"
    assert settings(recipe_id, {"approver_ids": ["502", "502", "501"]})["approver_ids"] == [
        "502",
        "501",
    ]
    assert settings(recipe_id, {"approver_ids": ["\uff15\uff10\uff12", "501"]})["approver_ids"] == [
        "502",
        "501",
    ]
    for aliases in (["0502", "502"], ["\uff15\uff10\uff12", "502"]):
        with pytest.raises(ValueError, match="duplicate"):
            settings(recipe_id, {"approver_ids": aliases})


@pytest.mark.parametrize(
    "values", [["0502"], ["\uff15\uff10\uff12"], ["502", " 502 "], [str(2**63)]]
)
def test_ticket_pool_does_not_reuse_task_or_approval_alias_rules(values):
    with pytest.raises(ValueError):
        settings("support.ticket-assignment", {"assignee_ids": values})


def test_ticket_pool_uses_numeric_sort_after_trimming():
    assert settings("support.ticket-assignment", {"assignee_ids": [" 11 ", "2"]}) == {
        "assignee_ids": ["2", "11"]
    }


def test_task_titles_collide_but_task_name_filters_deduplicate_after_trim():
    values = {"assignee_ids": ["502"], "task_titles": [" Start ", "Start"]}
    for recipe_id in ("projects.project-task-checklist", "hr.employee-onboarding"):
        with pytest.raises(ValueError, match="collide"):
            settings(recipe_id, values)
    assert settings(
        "projects.milestone-reminders",
        {"assignee_ids": ["502"], "task_names": [" Start ", "Start"]},
    )["task_names"] == ["Start"]


@pytest.mark.parametrize("key,maximum", [("task_names", 20), ("open_statuses", 50)])
def test_native_list_limit_precedes_alias_deduplication(key, maximum):
    supplied = {"assignee_ids": ["502"], "task_names": ["Launch"]}
    supplied[key] = [" " * index + "Launch" for index in range(maximum + 1)]
    with pytest.raises(ValueError, match=f"at most {maximum}"):
        settings("projects.milestone-reminders", supplied)


@pytest.mark.parametrize("recipe_id", ["projects.project-task-checklist", "hr.employee-onboarding"])
def test_checklists_reject_more_than_twenty_distinct_titles(recipe_id):
    with pytest.raises(ValueError, match="at most 20"):
        settings(
            recipe_id, {"assignee_ids": ["502"], "task_titles": [f"Task {i}" for i in range(21)]}
        )


def test_stage_uuid_aliases_normalize_while_won_stage_text_is_preserved():
    stage = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert settings(
        "sales.stalled-deal-follow-up",
        {"assignee_ids": ["502"], "open_stages": [stage.upper(), stage.replace("-", "")]},
    )["open_stages"] == [stage]
    for recipe_id in ("sales.won-deal-to-order", "cross-department.sales-delivery-billing"):
        supplied = {"won_stage": "  Closed Won  "}
        if recipe_id.startswith("cross"):
            supplied["assignee_ids"] = ["502"]
        assert settings(recipe_id, supplied)["won_stage"] == "  Closed Won  "


def test_sales_delivery_enforces_its_narrower_stage_limit():
    assert settings("sales.won-deal-to-order", {"won_stage": "x" * 201})["won_stage"] == "x" * 201
    with pytest.raises(ValueError, match="200"):
        settings(
            "cross-department.sales-delivery-billing",
            {"won_stage": "x" * 201, "assignee_ids": ["502"]},
        )


@pytest.mark.parametrize(
    "key,value",
    [
        ("weekdays", ["MON"]),
        ("weekdays", [" mon"]),
        ("weekdays", []),
        ("excluded_dates", ["20260901"]),
        ("excluded_dates", ["2026-02-30"]),
        ("employee_ids", ["not-a-member"]),
        ("lookback_days", 32),
        ("lookback_days", True),
    ],
)
def test_attendance_requires_explicit_native_calendar_and_member_values(key, value):
    supplied = {"assignee_ids": ["502"], "employee_ids": ["701"], key: value}
    with pytest.raises(ValueError):
        settings("hr.missing-attendance-reminders", supplied)


PRICE_INPUT = {
    "inventory_id": "11111111-1111-4111-8111-111111111111",
    "supplier_id": "22222222-2222-4222-8222-222222222222",
    "assignee_ids": ["502"],
    "reorder_quantity": 12,
    "currency": "JPY",
}


@pytest.mark.parametrize(
    "value,expected",
    [
        ("+050.20", "50.20"),
        ("1e2", "100"),
        ("1_000", "1000"),
        ("-0", "-0"),
        ("0.000001", "0.000001"),
    ],
)
def test_price_normalization_preserves_exact_decimal_without_float_or_rounding(value, expected):
    supplied = {**PRICE_INPUT, "unit_price": value}
    before = copy.deepcopy(supplied)
    assert settings("cross-department.replenishment-purchasing", supplied)["unit_price"] == expected
    assert supplied == before


@pytest.mark.parametrize(
    "value",
    [
        "NaN",
        "Infinity",
        "-1",
        "1000000001",
        "0.0000000",
        "1.0000001",
        "0e1000000000",
        "-0e1000000000",
        "0" * 31,
        0.1,
    ],
)
def test_price_rejects_out_of_range_precision_and_unbounded_expansion(value):
    with pytest.raises(ValueError):
        settings("cross-department.replenishment-purchasing", {**PRICE_INPUT, "unit_price": value})


def test_notes_keep_null_empty_and_whitespace_as_distinct_requested_settings():
    for value in (None, "", "  Terms\n "):
        assert (
            settings("billing.order-to-invoice", {"invoice_notes": value})["invoice_notes"] == value
        )
    assert (
        settings("purchasing.purchase-order-to-bill", {"bill_notes": "  Terms\n "})["bill_notes"]
        == "  Terms\n "
    )


def test_fixed_schedule_recipe_rejects_invented_ai_hour_or_native_payload_fields():
    supplied = {"assignee_ids": ["502"]}
    for extra in ({"local_hour": 17}, {"domain_handler": {"key": "arbitrary"}}, {"activate": True}):
        with pytest.raises(ValueError, match="Unknown template"):
            settings("support.unresolved-ticket-escalation", supplied | extra)

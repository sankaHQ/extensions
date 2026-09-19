# SPDX-License-Identifier: Apache-2.0
"""Explicit recipe-to-profile bindings for the next Business Flow release.

These candidate bindings require SDK a7. They are not advertised by the a1
entrypoint or manifest. Each binding pins a recipe selector and stable logical
identity; user settings and native database IDs never choose that identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sanka_extension_business_flows.catalog import recipe

type Family = Literal["conversion", "approval", "task", "process"]


@dataclass(frozen=True, slots=True)
class NativeRecipe:
    recipe_id: str
    family: Family
    variant: str

    @property
    def workflow_id(self) -> str:
        return self.recipe_id.replace(".", "-")

    @property
    def selector(self) -> str:
        return f"sanka/{self.workflow_id}"

    def identity_fields(self) -> dict[str, str]:
        return {
            "recipe_id": self.recipe_id,
            "family": self.family,
            "variant": self.variant,
            "workflow_id": self.workflow_id,
            "selector": self.selector,
        }


NATIVE_RECIPES: tuple[NativeRecipe, ...] = (
    NativeRecipe("sales.deal-to-estimate", "conversion", "deal_to_estimate"),
    NativeRecipe("sales.won-deal-to-order", "conversion", "won_deal_to_order"),
    NativeRecipe("sales.stalled-deal-follow-up", "task", "stalled_deal_follow_up"),
    NativeRecipe("billing.order-to-invoice", "conversion", "order_to_invoice"),
    NativeRecipe("billing.monthly-invoices", "process", "monthly_invoice_consolidation"),
    NativeRecipe("billing.overdue-reminders", "task", "overdue_invoice_reminder"),
    NativeRecipe("purchasing.purchase-order-to-bill", "conversion", "purchase_order_to_bill"),
    NativeRecipe("purchasing.purchase-approvals", "approval", "purchase_order"),
    NativeRecipe("purchasing.supplier-payment-reminders", "task", "supplier_payment_reminder"),
    NativeRecipe("inventory.order-to-stock-movement", "conversion", "order_to_stock_out"),
    NativeRecipe("inventory.low-stock-replenishment", "task", "low_stock_replenishment"),
    NativeRecipe("inventory.stock-discrepancy-review", "task", "stock_discrepancy_review"),
    NativeRecipe("expenses.expense-approvals", "approval", "expense"),
    NativeRecipe("expenses.reimbursement-preparation", "task", "expense_reimbursement_preparation"),
    NativeRecipe("expenses.missing-receipt-reminders", "task", "missing_expense_receipt"),
    NativeRecipe("projects.project-task-checklist", "task", "project_task_checklist"),
    NativeRecipe("projects.milestone-reminders", "task", "milestone_reminder"),
    NativeRecipe("projects.overdue-task-escalation", "task", "overdue_task_escalation"),
    NativeRecipe("support.ticket-assignment", "process", "ticket_assignment"),
    NativeRecipe("support.unresolved-ticket-escalation", "task", "unresolved_ticket_escalation"),
    NativeRecipe("support.resolution-follow-up", "task", "ticket_resolution_follow_up"),
    NativeRecipe("hr.employee-onboarding", "task", "employee_onboarding"),
    NativeRecipe("hr.leave-approvals", "approval", "absence"),
    NativeRecipe("hr.missing-attendance-reminders", "task", "missing_attendance"),
    NativeRecipe("cross-department.sales-delivery-billing", "process", "sales_delivery_billing"),
    NativeRecipe(
        "cross-department.replenishment-purchasing", "process", "replenishment_purchasing"
    ),
    NativeRecipe("cross-department.expenses-accounting", "task", "expense_accounting_review"),
)


def native_recipe(recipe_id: str) -> NativeRecipe:
    for selected in NATIVE_RECIPES:
        if selected.recipe_id == recipe_id:
            # Fail closed if the package's catalog and bindings drift apart.
            recipe(recipe_id)
            return selected
    raise ValueError("Unknown native business recipe")


def recipe_for_selector(selector: str) -> NativeRecipe:
    for selected in NATIVE_RECIPES:
        if selected.selector == selector:
            return selected
    raise ValueError("Unknown native business selector")

# Native recipe family contracts

SDK a7 candidate defines construction contracts for the 27 catalog recipes beyond
HubSpot order billing. These are declarative definitions, not migrated generators
or evidence of runtime readiness. The business-flow extension must generate each
recipe through its exact selector, and the private host must implement and prove
the corresponding behavior. Provider implementations remain private.

## Recipe migration map

The table is the intended generator mapping. These selectors are not newly
advertised by the currently published Business Flow package.

| Catalog recipe | Profile | Discriminator |
| --- | --- | --- |
| `sales.deal-to-estimate` | Record conversion | `deal_to_estimate` |
| `sales.won-deal-to-order` | Record conversion | `won_deal_to_order` |
| `billing.order-to-invoice` | Record conversion | `order_to_invoice` |
| `purchasing.purchase-order-to-bill` | Record conversion | `purchase_order_to_bill` |
| `inventory.order-to-stock-movement` | Record conversion | `order_to_stock_out` |
| `purchasing.purchase-approvals` | Source approval | `purchase_order` |
| `expenses.expense-approvals` | Source approval | `expense` |
| `hr.leave-approvals` | Source approval | `absence` |
| `sales.stalled-deal-follow-up` | Assigned task | `stalled_deal_follow_up` |
| `billing.overdue-reminders` | Assigned task | `overdue_invoice_reminder` |
| `purchasing.supplier-payment-reminders` | Assigned task | `supplier_payment_reminder` |
| `inventory.low-stock-replenishment` | Assigned task | `low_stock_replenishment` |
| `inventory.stock-discrepancy-review` | Assigned task | `stock_discrepancy_review` |
| `expenses.reimbursement-preparation` | Assigned task | `expense_reimbursement_preparation` |
| `expenses.missing-receipt-reminders` | Assigned task | `missing_expense_receipt` |
| `projects.project-task-checklist` | Assigned task | `project_task_checklist` |
| `projects.milestone-reminders` | Assigned task | `milestone_reminder` |
| `projects.overdue-task-escalation` | Assigned task | `overdue_task_escalation` |
| `support.unresolved-ticket-escalation` | Assigned task | `unresolved_ticket_escalation` |
| `support.resolution-follow-up` | Assigned task | `ticket_resolution_follow_up` |
| `hr.employee-onboarding` | Assigned task | `employee_onboarding` |
| `hr.missing-attendance-reminders` | Assigned task | `missing_attendance` |
| `cross-department.expenses-accounting` | Assigned task | `expense_accounting_review` |
| `billing.monthly-invoices` | Business process | `monthly_invoice_consolidation` |
| `support.ticket-assignment` | Business process | `ticket_assignment` |
| `cross-department.sales-delivery-billing` | Business process | `sales_delivery_billing` |
| `cross-department.replenishment-purchasing` | Business process | `replenishment_purchasing` |

## Selections and identity

Profiles contain fully resolved settings, with no omitted defaults. Unknown fields
and cross-recipe options fail validation. User and record IDs are canonical; list
inputs are already trimmed and unique. Consumers must normalize legacy input before
creating a profile and show the effective values in the plan. They must not silently
change an existing managed configuration while refreshing a template.

Approval selection order is meaningful and retained. Ticket assignment uses a
numerically ascending pool of workspace-user IDs, matching the current native
round-robin implementation; the selected input list is not a priority sequence.
Task titles retain order. Setting changes leave logical node identities unchanged.
Native IDs are host-owned bindings and cannot be reconstructed from a title or
list position.

## Scheduled tasks

The current native daily trigger resolves the selected workspace-local hour to a
UTC minute using the workspace offset at construction. These profiles explicitly
retain that behavior. They do not promise automatic daylight-saving rescheduling;
hosts must bind the observed timezone and resolved minute in their reviewed plan
and reject stale observations at construction. Calendar selection and task due
dates use the workspace timezone at execution.

Seven follow-up recipes expose `local_hour`; the other scheduled task recipes run
at the existing default of 09:00. Project checklists use `project.created` and have
no schedule. Follow-ups create assigned tasks without sending messages, posting
journals, making payments or changing source status.

Receipts are scoped to workspace, workflow, node, source and the recipe's source
revision. A host must retain that distinction on retries. Ordinary follow-ups
commit each source task and receipt together, with a limit of 500 tasks per run.
Onboarding commits a complete checklist per employee and permits 500 employees;
the project checklist is one atomic source operation. A later failure retains
completed sources so a retry can continue. Task and receipt IDs must be verified
through real native execution before advertising these guarantees.

Missing attendance requires complete Attendance and Absence visibility to infer
missing records. It checks completed selected local workdays, excludes selected
dates, actual entries and approved leave, and records a receipt per employee/day.

## Other business processes

Monthly billing considers the previous complete local calendar month from the
selected billing day onward. It selects unbilled orders and creates draft invoices
grouped by customer. More than 5,000 selected orders fails before writing. A failure
after some customer groups complete retains those groups; retries select remaining
unbilled orders. This is not an atomic transaction across the whole monthly batch.

Sales delivery billing prepares an order, a delivery-preparation task and a draft
invoice atomically with its receipt. It preserves existing linked orders/invoices
and does not wait for delivery completion. Replenishment purchasing creates a draft
purchase order and receiving task for the selected inventory. An outstanding
purchase suppresses another purchase until it is in stock or archived; payment
alone does not establish receipt.

## Admission and follow-up work

Blueprint v5 is construction-only. It accepts one closed native workflow, rejects
portable references and verification scenarios, and requires capabilities for its
specific profile and behavior. An extension cannot inject native action payloads,
SQL, arbitrary handlers or provider credentials. Blueprint responses preserve the
complete requested profile, including stable node identities and fixed policies.

V1–v4 retain their existing contracts. HubSpot billing stays on v3/v4; its eleven
verification cases do not prove conversions, approval decisions, task receipts or
monthly invoice grouping. Runtime admission, private translation parity, each
recipe generator and profile-specific verification/activation are separate work.
Publish approved SDK a6, then this reviewed successor, before consumer dependency
or manifest updates.

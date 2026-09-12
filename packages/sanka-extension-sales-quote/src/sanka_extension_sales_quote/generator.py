# SPDX-License-Identifier: Apache-2.0
"""Generate the declared Sales configuration without reading or mutating a target."""

from __future__ import annotations

from sanka_extension_sales_quote.metadata import (
    CAPABILITY,
    EXTENSION_ID,
    VERSION,
    WORKFLOW_ID,
    template_identity,
)
from sanka_extensions.flow import (
    Action,
    AssociationMapping,
    Blueprint,
    BlueprintOrigin,
    BlueprintRequest,
    Condition,
    FieldMapping,
    Reference,
    Resource,
    Scenario,
    ScenarioEvent,
    Trigger,
    ValueBinding,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    artifact_digest,
)
from sanka_extensions.flow.definition import JsonValue


def _validate(request: BlueprintRequest) -> None:
    CAPABILITY.validate_request(request)
    if (request.extension.id, request.extension.revision) != (EXTENSION_ID, VERSION):
        raise ValueError("The requested extension identity differs from this executable")
    parameters = request.definition.parameters
    if set(parameters) != {"references", "values"} or type(parameters["references"]) is not list:
        raise ValueError("Sales parameters must contain exactly references and values")
    parameter_references = tuple(
        sorted(
            (Reference.from_dict(value) for value in parameters["references"]), key=lambda r: r.id
        )
    )
    if parameter_references != request.references:
        raise ValueError("Selected target references differ from the requested Sales parameters")
    if artifact_digest(parameters["values"]) != artifact_digest(request.values):
        raise ValueError("Selected stage/status values differ from the requested Sales parameters")
    if request.values["quote_stage"] == request.values["other_stage"]:
        raise ValueError("The selected Quote stage and comparison stage must differ")


def generate_blueprint(request: BlueprintRequest) -> Blueprint:
    """One draft Quote per Deal, with verified activation required by the runtime.

    Native field types, required fields, stage choices, draft semantics and record
    associations must be checked by the host against the pinned target revision.
    This function does not implement a trigger, action executor or API client.
    """
    _validate(request)
    fields = (
        FieldMapping("quote.status", ValueBinding("literal", value=request.values["draft_status"])),
        FieldMapping(
            "quote.title", ValueBinding("event_field", field_ref="deal.title", phase="after")
        ),
    )
    associations = (AssociationMapping("quote.deal", ValueBinding("event_record")),)
    graph = WorkflowGraph(
        WORKFLOW_ID,
        (
            WorkflowNode("deal.updated", Trigger("deal", ("deal.stage",))),
            WorkflowNode(
                "stage.is-quote",
                Condition(
                    ValueBinding("event_field", field_ref="deal.stage", phase="after"),
                    ValueBinding("literal", value=request.values["quote_stage"]),
                ),
            ),
            WorkflowNode("quote.create", Action("quote", fields, associations)),
        ),
        (
            WorkflowEdge("deal.updated", "stage.is-quote"),
            WorkflowEdge("stage.is-quote", "quote.create", "true"),
        ),
    )
    title = "Sales Flow verification"
    before: dict[str, JsonValue] = {
        "deal.stage": request.values["other_stage"],
        "deal.title": title,
    }
    after: dict[str, JsonValue] = {"deal.stage": request.values["quote_stage"], "deal.title": title}
    match = ScenarioEvent("sales.quote-event", "sales.scenario-deal", before, after)
    no_match = ScenarioEvent("sales.away-event", "sales.scenario-deal", after, before)
    unchanged = ScenarioEvent("sales.unchanged-event", "sales.scenario-deal", after, after)
    expected = (
        FieldMapping("quote.status", ValueBinding("literal", value=request.values["draft_status"])),
        FieldMapping("quote.title", ValueBinding("literal", value=title)),
    )
    origin = template_identity()
    return Blueprint(
        id=EXTENSION_ID,
        revision=origin.revision,
        origin=BlueprintOrigin("template", origin),
        extension=request.extension,
        resources=(Resource(WORKFLOW_ID, "workflow", graph.to_dict()),),
        references=request.references,
        parameters=request.blueprint_parameters,
        scenarios=(
            Scenario("sales.no-match-away", WORKFLOW_ID, "no_match", (no_match,), 0),
            Scenario("sales.no-match-unchanged", WORKFLOW_ID, "no_match", (unchanged,), 0),
            Scenario("sales.match", WORKFLOW_ID, "match", (match,), 1, expected, associations),
            Scenario(
                "sales.retry", WORKFLOW_ID, "retry", (match, match), 1, expected, associations
            ),
        ),
    )

# SPDX-License-Identifier: Apache-2.0
"""Creation semantics are versioned and require explicit native capabilities."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from sanka_extensions import flow


def test_documented_creation_fixture():
    path = Path(__file__).parent / "fixtures/synthetic_sales_created_estimate_blueprint.json"
    payload = json.loads(path.read_text())
    blueprint = flow.Blueprint.from_dict(payload)
    assert blueprint.to_dict() == payload
    assert blueprint.resources[0].id == "sales.estimate"
    assert len(blueprint.resources[0].spec["nodes"]) == 2
    blueprint.require_supported(capabilities=frozenset(blueprint.required_capabilities))


@pytest.fixture
def creation():
    payload = json.loads(
        (Path(__file__).parent / "fixtures/synthetic_sales_quote_blueprint.json").read_text()
    )
    graph = payload["resources"][0]["spec"]
    graph["schema_version"] = "sanka-flow-graph/v2"
    graph["nodes"] = [node for node in graph["nodes"] if node["kind"] != "condition"]
    trigger = next(n for n in graph["nodes"] if n["kind"] == "trigger")
    action = next(n for n in graph["nodes"] if n["kind"] == "action")
    trigger["spec"].update(operation="record.created", changed_fields=[])
    graph["edges"] = [{"source": trigger["id"], "target": action["id"], "when": "always"}]
    payload["schema_version"] = flow.BLUEPRINT_V2_SCHEMA_VERSION
    for scenario in payload["scenarios"]:
        if scenario["case"] != "no_match":
            for event in scenario["events"]:
                event.update(operation="record.created", before={})
    payload["required_capabilities"] = sorted(
        {
            flow.BLUEPRINT_V2_SCHEMA_VERSION,
            "flow.resource.workflow/v1",
            *flow.WorkflowGraph.from_dict(graph).required_capabilities,
        }
    )
    return payload


def test_creation_round_trip_and_host_capability_gate(creation):
    blueprint = flow.Blueprint.from_dict(creation)
    assert blueprint.to_dict() == creation
    with pytest.raises(ValueError, match="Target lacks"):
        blueprint.require_supported()
    capabilities = frozenset(blueprint.required_capabilities)
    for capability in capabilities:
        with pytest.raises(ValueError, match="Target lacks"):
            blueprint.require_supported(capabilities=capabilities - {capability})
    blueprint.require_supported(capabilities=capabilities)
    assert "flow.trigger.record.created/v1" in capabilities
    assert "flow.condition.equals/v1" not in capabilities


@pytest.mark.parametrize("conditional", [False, True])
@pytest.mark.parametrize("operation", ["record.updated", "record.created"])
def test_v2_supports_each_bounded_graph_shape(operation, conditional):
    trigger = flow.WorkflowNode(
        "trigger",
        flow.Trigger("deal", ("stage",) if operation == "record.updated" else (), operation),
    )
    action = flow.WorkflowNode(
        "action",
        flow.Action(
            "estimate", (flow.FieldMapping("status", flow.ValueBinding("literal", value="draft")),)
        ),
    )
    nodes = (trigger, action)
    edges = (flow.WorkflowEdge("trigger", "action", "always"),)
    if conditional:
        condition = flow.WorkflowNode(
            "condition",
            flow.Condition(
                flow.ValueBinding("event_field", field_ref="stage", phase="after"),
                flow.ValueBinding("literal", value="Quote"),
            ),
        )
        nodes += (condition,)
        edges = (
            flow.WorkflowEdge("trigger", "condition", "always"),
            flow.WorkflowEdge("condition", "action", "true"),
        )
    graph = flow.WorkflowGraph("sales", nodes, edges, "sanka-flow-graph/v2")
    assert flow.WorkflowGraph.from_dict(graph.to_dict()) == graph
    assert ("flow.condition.equals/v1" in graph.required_capabilities) == conditional


@pytest.mark.parametrize(
    "mutation", ["capability", "v1", "before", "watched", "branch", "event", "no_match", "retry"]
)
def test_creation_rejects_incompatible_or_incomplete_semantics(creation, mutation):
    graph = creation["resources"][0]["spec"]
    trigger = next(n for n in graph["nodes"] if n["kind"] == "trigger")
    match = next(s for s in creation["scenarios"] if s["case"] == "match")
    if mutation == "capability":
        creation["required_capabilities"].remove("flow.trigger.record.created/v1")
    elif mutation == "v1":
        del graph["schema_version"]
    elif mutation == "before":
        match["events"][0]["before"] = {"deal.stage": "old"}
    elif mutation == "watched":
        trigger["spec"]["changed_fields"] = ["deal.stage"]
    elif mutation == "branch":
        graph["edges"][0]["when"] = "true"
    elif mutation == "event":
        del match["events"][0]["operation"]
    elif mutation == "no_match":
        no_match = next(s for s in creation["scenarios"] if s["case"] == "no_match")
        no_match["events"] = deepcopy(match["events"])
    elif mutation == "retry":
        creation["scenarios"] = [s for s in creation["scenarios"] if s["case"] != "retry"]
    with pytest.raises(ValueError):
        flow.Blueprint.from_dict(creation)


def test_creation_cannot_bind_nonexistent_before_state(creation):
    graph = creation["resources"][0]["spec"]
    action = next(n for n in graph["nodes"] if n["kind"] == "action")
    action["spec"]["fields"][1]["value"]["phase"] = "before"
    with pytest.raises(ValueError, match="cannot read before"):
        flow.Blueprint.from_dict(creation)


def test_v1_cannot_smuggle_creation_events(creation):
    original = json.loads(
        (Path(__file__).parent / "fixtures/synthetic_sales_quote_blueprint.json").read_text()
    )
    original["scenarios"] = creation["scenarios"]
    with pytest.raises(ValueError, match="schema_version v2"):
        flow.Blueprint.from_dict(original)

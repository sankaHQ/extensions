# SPDX-License-Identifier: Apache-2.0
"""Reusable template generation and real subprocess contract acceptance."""

import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from sanka_extension_sales_quote import generate_blueprint
from sanka_extension_sales_quote.metadata import (
    CAPABILITY,
    EXTENSION_ID,
    VERSION,
    template_identity,
)

from sanka_extensions import flow

PACKAGE = Path(__file__).parents[1]


@pytest.fixture
def flow_request():
    references = (
        flow.Reference("deal", "object", "deal"),
        flow.Reference("deal.stage", "property", "stage", "deal"),
        flow.Reference("deal.title", "property", "name", "deal"),
        flow.Reference("quote", "object", "estimate"),
        flow.Reference("quote.status", "property", "status", "quote"),
        flow.Reference("quote.title", "property", "notes", "quote"),
        flow.Reference(
            "quote.deal",
            "relationship",
            "00000000-0000-4000-8000-000000000001",
            "quote",
            related_object_id="deal",
        ),
    )
    values = {"quote_stage": "quote", "other_stage": "negotiation", "draft_status": "draft"}
    manifest = json.loads((PACKAGE / "extension.json").read_text())
    definition = flow.create(
        type=EXTENSION_ID,
        parameters={"references": [r.to_dict() for r in references], "values": values},
    )
    return flow.BlueprintRequest(
        "sales-generate",
        flow.ArtifactIdentity(EXTENSION_ID, VERSION, flow.artifact_digest(manifest)),
        definition,
        flow.TargetIdentity("synthetic-workspace", "synthetic-revision"),
        references,
        values,
    )


def test_sales_generates_one_inactive_workflow_with_exact_fields_and_deal_link(flow_request):
    blueprint = generate_blueprint(flow_request)
    flow.BlueprintResponse.success(flow_request, blueprint).validate_for(flow_request)
    assert blueprint.origin.identity == template_identity()
    assert len(blueprint.resources) == 1
    assert blueprint.resources[0].id == "sales.quote"
    graph = flow.WorkflowGraph.from_dict(blueprint.resources[0].spec)
    trigger = next(node.spec for node in graph.nodes if node.kind == "trigger")
    action = next(node.spec for node in graph.nodes if node.kind == "action")
    assert trigger.changed_fields == ("deal.stage",)
    fields = {mapping.field_ref: mapping.value.to_dict() for mapping in action.fields}
    assert fields == {
        "quote.status": {"kind": "literal", "value": "draft"},
        "quote.title": {"kind": "event_field", "field_ref": "deal.title", "phase": "after"},
    }
    assert action.associations == (
        flow.AssociationMapping("quote.deal", flow.ValueBinding("event_record")),
    )
    assert action.identity == flow.RecordIdentity()
    assert blueprint.policies["lifecycle"]["new_automations"] == "disabled_until_activation"
    assert blueprint.parameters == flow_request.blueprint_parameters


def test_scenarios_cover_false_condition_unchanged_stage_match_and_retry(flow_request):
    scenarios = {s.id: s for s in generate_blueprint(flow_request).scenarios}
    assert set(scenarios) == {
        "sales.no-match-away",
        "sales.no-match-unchanged",
        "sales.match",
        "sales.retry",
    }
    unchanged = scenarios["sales.no-match-unchanged"]
    assert unchanged.expected_created_count == 0
    assert unchanged.events[0].before == unchanged.events[0].after
    away = scenarios["sales.no-match-away"]
    assert away.events[0].after["deal.stage"] == "negotiation"
    assert away.expected_created_count == 0
    retry = scenarios["sales.retry"]
    assert retry.expected_created_count == 1 and retry.events[0] == retry.events[1]
    assert all(s.required for s in scenarios.values())


def test_generation_is_deterministic_and_keeps_caller_objects_immutable(flow_request):
    original = flow_request.to_dict()
    first, second = generate_blueprint(flow_request), generate_blueprint(flow_request)
    assert first.digest == second.digest
    first.resources[0].spec.clear()
    first.parameters.clear()
    assert flow_request.to_dict() == original
    assert generate_blueprint(flow_request) == second


def test_selected_values_are_substituted_without_defaulting_or_coercion(flow_request):
    payload = flow_request.to_dict()
    values = {"quote_stage": "見積待ち", "other_stage": "商談中", "draft_status": "Draft selected"}
    payload["values"] = values
    payload["definition"]["parameters"]["values"] = values
    request = flow.BlueprintRequest.from_dict(payload)
    blueprint = generate_blueprint(request)
    graph = flow.WorkflowGraph.from_dict(blueprint.resources[0].spec)
    condition = next(node.spec for node in graph.nodes if node.kind == "condition")
    action = next(node.spec for node in graph.nodes if node.kind == "action")
    assert condition.right == flow.ValueBinding("literal", value="見積待ち")
    status = next(mapping for mapping in action.fields if mapping.field_ref == "quote.status")
    assert status.value == flow.ValueBinding("literal", value="Draft selected")
    assert blueprint.parameters == request.blueprint_parameters


def test_bundled_template_descriptor_matches_the_generated_semantics(flow_request):
    template = json.loads((PACKAGE / "src/sanka_extension_sales_quote/template.json").read_text())
    blueprint = generate_blueprint(flow_request)
    graph = flow.WorkflowGraph.from_dict(blueprint.resources[0].spec)
    trigger = next(node.spec for node in graph.nodes if node.kind == "trigger")
    condition = next(node.spec for node in graph.nodes if node.kind == "condition")
    action = next(node.spec for node in graph.nodes if node.kind == "action")
    assert template["id"] == blueprint.id
    assert template["revision"] == blueprint.revision
    assert template["workflow_id"] == graph.id
    assert template["trigger"] == {
        "operation": trigger.operation,
        "object_role": trigger.object_ref,
        "changed_field_role": trigger.changed_fields[0],
    }
    assert template["condition"] == {
        "operator": condition.operator,
        "field_role": condition.left.to_dict()["field_ref"],
        "phase": condition.left.to_dict()["phase"],
        "selected_value": "quote_stage",
    }
    assert template["action"]["operation"] == action.operation
    assert template["action"]["object_role"] == action.object_ref
    assert template["action"]["identity"] == action.identity.to_dict()
    assert template["required_cases"] == ["no_match_away", "no_match_unchanged", "match", "retry"]
    assert blueprint.origin.identity.digest == flow.artifact_digest(template)


@pytest.mark.parametrize("change", ["role_key", "value", "extra_parameter"])
def test_envelope_cannot_silently_override_definition_parameters(flow_request, change):
    payload = flow_request.to_dict()
    if change == "role_key":
        payload["references"][0]["key"] = "other-deal-object"
    elif change == "value":
        payload["values"]["quote_stage"] = "another-stage"
    else:
        payload["definition"]["parameters"]["unknown"] = True
    with pytest.raises(ValueError):
        generate_blueprint(flow.BlueprintRequest.from_dict(payload))


def test_selected_stages_must_differ_and_have_native_string_type(flow_request):
    for value in ("negotiation", 1, None):
        payload = flow_request.to_dict()
        payload["values"]["quote_stage"] = value
        payload["definition"]["parameters"]["values"] = deepcopy(payload["values"])
        with pytest.raises(ValueError):
            generate_blueprint(flow.BlueprintRequest.from_dict(payload))


def test_wrong_installed_extension_version_fails_before_generation(flow_request):
    payload = flow_request.to_dict()
    payload["extension"] = flow.ArtifactIdentity(
        EXTENSION_ID, "0.1.0a2", "sha256:" + "0" * 64
    ).to_dict()
    with pytest.raises(ValueError, match="extension identity"):
        generate_blueprint(flow.BlueprintRequest.from_dict(payload))


def run_generator(tmp_path, payload):
    return subprocess.run(
        [sys.executable, "-I", "-m", "sanka_extension_sales_quote"],
        input=payload,
        capture_output=True,
        cwd=tmp_path,
        env={"PATH": str(Path(sys.executable).parent), "PYTHONIOENCODING": "utf-8"},
        timeout=10,
        check=False,
    )


def test_actual_subprocess_emits_one_correlated_response_and_no_working_files(
    flow_request, tmp_path
):
    encoded = flow.encode_message(flow_request)
    first = run_generator(tmp_path, encoded)
    second = run_generator(tmp_path, encoded)
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout and first.stderr == b""
    assert first.stdout.count(b"\n") == 1
    response = flow.BlueprintResponse.from_dict(flow.decode_message(first.stdout))
    response.validate_for(flow_request)
    assert response.blueprint == generate_blueprint(flow_request)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "message",
    [
        b"{}",
        b"{}{}",
        b'{"schema_version":1,"schema_version":2}',
        b'{"secret":"not-an-input"}',
        b" " * (flow.MAX_MESSAGE_BYTES + 1),
    ],
)
def test_invalid_protocol_never_returns_a_blueprint_or_echoes_input(tmp_path, message):
    result = run_generator(tmp_path, message)
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == b"Invalid Flow protocol request.\n"


def test_handled_template_failure_is_a_valid_correlated_error(flow_request, tmp_path):
    payload = flow_request.to_dict()
    payload["definition"]["parameters"]["values"]["quote_stage"] = "conflicting-choice"
    invalid = flow.BlueprintRequest.from_dict(payload)
    result = run_generator(tmp_path, flow.encode_message(invalid))
    assert result.returncode == 1 and result.stderr == b""
    response = flow.BlueprintResponse.from_dict(flow.decode_message(result.stdout))
    response.validate_for(invalid)
    assert response.error.code == "FLOW_INPUT_INVALID"
    assert response.blueprint is None


def test_static_manifest_capability_matches_generator_and_uses_supplement_only():
    manifest = json.loads((PACKAGE / "extension.json").read_text())
    assert manifest["kind"] == "flow"
    assert manifest["commands"] == ["blueprint"]
    assert manifest["protocol_version"] == flow.PROTOCOL_VERSION
    assert manifest["capabilities"] == [CAPABILITY.to_dict()]
    assert manifest["runtime"] == {"sanka_cli": ">=0.2.10,<0.3"}
    root = PACKAGE.parents[1]
    legacy = json.loads((root / "marketplace.json").read_text())
    supplement = json.loads((root / "flow-marketplace.json").read_text())
    assert EXTENSION_ID not in {item["id"] for item in legacy["extensions"]}
    assert supplement == {
        "schema_version": "sanka-marketplace/v1",
        "extensions": [
            {"id": EXTENSION_ID, "manifest": "packages/sanka-extension-sales-quote/extension.json"}
        ],
    }


def test_declared_dependency_is_only_published_sdk_candidate():
    import tomllib

    project = tomllib.loads((PACKAGE / "pyproject.toml").read_text())["project"]
    assert project["dependencies"] == ["sanka-extension-sdk==0.1.0a3"]
    assert project["version"] == VERSION
    assert os.environ.get("PYTHONPATH") is None or "sanka-api" not in os.environ["PYTHONPATH"]

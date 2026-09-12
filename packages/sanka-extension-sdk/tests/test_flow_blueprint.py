# SPDX-License-Identifier: Apache-2.0
"""Flow artifacts fail closed before a host can plan or construct resources."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from sanka_extensions import flow

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_sales_quote_blueprint.json"


@pytest.fixture
def payload():
    return json.loads(FIXTURE.read_text())


def node(payload, kind):
    return next(n for n in payload["resources"][0]["spec"]["nodes"] if n["kind"] == kind)


def scenario(payload, case):
    return next(s for s in payload["scenarios"] if s["case"] == case)


def reference(payload, id):
    return next(r for r in payload["references"] if r["id"] == id)


def test_sales_fixture_round_trip_and_canonical_identity(payload):
    blueprint = flow.Blueprint.from_dict(payload)
    assert blueprint.to_dict() == payload
    assert (
        blueprint.digest
        == "sha256:8c48cb388d4e055caa689ae741f480728cac0c4dcd287b4462ca41009a3b2b52"
    )
    blueprint.require_supported()
    payload["references"].reverse()
    payload["scenarios"].reverse()
    payload["resources"][0]["spec"]["nodes"].reverse()
    payload["resources"][0]["spec"]["edges"].reverse()
    assert flow.Blueprint.from_dict(payload) == blueprint
    assert flow.Blueprint.from_dict(payload).digest == blueprint.digest


def test_blueprint_owns_nested_input_and_output_copies(payload):
    blueprint = flow.Blueprint.from_dict(payload)
    original = deepcopy(payload)
    node(payload, "condition")["spec"]["right"]["value"] = "Closed"
    payload["parameters"]["fixture_only"] = False
    scenario(payload, "match")["events"][0]["after"]["deal.stage"] = "Closed"
    output = blueprint.to_dict()
    output["resources"].clear()
    blueprint.resources[0].spec["nodes"].clear()
    blueprint.parameters.clear()
    blueprint.scenarios[0].events[0].after.clear()
    assert blueprint.to_dict() == original


@pytest.mark.parametrize(
    "field", ["schema_version", "origin", "extension", "policies", "scenarios"]
)
def test_blueprint_rejects_missing_fields(payload, field):
    del payload[field]
    with pytest.raises(ValueError, match="unexpected or missing"):
        flow.Blueprint.from_dict(payload)


@pytest.mark.parametrize(
    "location", ["blueprint", "resource", "node", "binding", "reference", "event"]
)
def test_unknown_fields_are_not_silently_dropped(payload, location):
    selected = {
        "blueprint": payload,
        "resource": payload["resources"][0],
        "node": node(payload, "trigger"),
        "binding": node(payload, "condition")["spec"]["left"],
        "reference": payload["references"][0],
        "event": scenario(payload, "match")["events"][0],
    }[location]
    selected["unknown"] = True
    with pytest.raises(ValueError, match="unexpected or missing"):
        flow.Blueprint.from_dict(payload)


@pytest.mark.parametrize(
    "version", [None, 1, "sanka-flow-blueprint/v2", "sanka-flow-definition/v1"]
)
def test_blueprint_requires_exact_schema_version(payload, version):
    payload["schema_version"] = version
    with pytest.raises(ValueError, match="schema_version"):
        flow.Blueprint.from_dict(payload)


def test_blueprint_keeps_published_definition_policies(payload):
    assert flow.Blueprint.from_dict(payload).policies == flow.create(type="crm").policies
    payload["policies"]["lifecycle"]["activation"] = "automatic"
    with pytest.raises(ValueError, match="policies"):
        flow.Blueprint.from_dict(payload)


@pytest.mark.parametrize("digest", ["a" * 64, "sha256:" + "A" * 64, "sha256:123", None])
def test_provenance_requires_explicit_sha256_identity(payload, digest):
    payload["extension"]["digest"] = digest
    with pytest.raises(ValueError, match="digest"):
        flow.Blueprint.from_dict(payload)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {1: "bad"}, ("tuple",), {"set"}])
def test_every_embedded_json_boundary_rejects_non_json(value):
    with pytest.raises(ValueError):
        flow.ValueBinding("literal", value=value)
    with pytest.raises(ValueError):
        flow.SourceNode("node", "action", "native.action", {"value": value})
    with pytest.raises(ValueError):
        flow.ScenarioEvent("event", "record", {}, {"property": value})


def test_json_cycles_rejected_but_shared_acyclic_values_supported():
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError, match="cycles"):
        flow.ValueBinding("literal", value=cyclic)
    shared = ["ok"]
    assert flow.ValueBinding("literal", value=[shared, shared]).to_dict()["value"] == [
        ["ok"],
        ["ok"],
    ]


def test_canonical_digest_does_not_coerce_boolean_number_or_string():
    assert len({flow.artifact_digest(v) for v in [True, 1, 1.0, "1", None]}) == 5
    assert flow.artifact_digest({"a": 1, "b": 2}) == flow.artifact_digest({"b": 2, "a": 1})


@pytest.mark.parametrize("operation", ["record.deleted", "http.request", "record.create"])
def test_unsupported_trigger_operation_rejected(payload, operation):
    node(payload, "trigger")["spec"]["operation"] = operation
    with pytest.raises(ValueError, match="trigger operation"):
        flow.Blueprint.from_dict(payload)


@pytest.mark.parametrize("operation", ["record.update", "send.email", "custom.code"])
def test_unsupported_action_operation_rejected(payload, operation):
    node(payload, "action")["spec"]["operation"] = operation
    with pytest.raises(ValueError, match="action operation"):
        flow.Blueprint.from_dict(payload)


def test_branch_and_duplicate_node_rejected(payload):
    graph = payload["resources"][0]["spec"]
    graph["edges"].append({"source": "stage.is-quote", "target": "deal.updated", "when": "true"})
    with pytest.raises(ValueError, match="without branches"):
        flow.Blueprint.from_dict(payload)
    graph["edges"].pop()
    graph["nodes"].append(deepcopy(graph["nodes"][0]))
    with pytest.raises(ValueError, match="unique identities"):
        flow.Blueprint.from_dict(payload)


@pytest.mark.parametrize(
    "field,value", [("on_existing", "overwrite"), ("on_missing", "recreate"), ("scope", "event")]
)
def test_record_identity_never_overwrites_or_recreates_bound_target(payload, field, value):
    node(payload, "action")["spec"]["identity"][field] = value
    with pytest.raises(ValueError):
        flow.Blueprint.from_dict(payload)


def test_record_identity_defaults_include_stage_reentry_scope():
    assert flow.RecordIdentity().to_dict() == {
        "scope": "installation_workflow_action_event_record",
        "on_existing": "preserve",
        "on_missing": "conflict",
    }


@pytest.mark.parametrize(
    "changed_fields", [[], ["deal.stage", "deal.stage"], ["missing"], ["quote.status"]]
)
def test_trigger_requires_unique_fields_from_event_object(payload, changed_fields):
    node(payload, "trigger")["spec"]["changed_fields"] = changed_fields
    with pytest.raises(ValueError):
        flow.Blueprint.from_dict(payload)


def test_relationship_endpoint_must_match_event_record_object(payload):
    reference(payload, "quote.deal")["related_object_id"] = "quote"
    with pytest.raises(ValueError, match="different related object"):
        flow.Blueprint.from_dict(payload)


def test_relationship_endpoint_must_be_an_object_in_same_scope(payload):
    reference(payload, "quote.deal")["related_object_id"] = "deal.stage"
    with pytest.raises(ValueError, match="object in the same scope"):
        flow.Blueprint.from_dict(payload)


@pytest.mark.parametrize(
    "binding",
    [
        flow.ValueBinding("literal", value="record"),
        flow.ValueBinding("event_field", field_ref="deal.title", phase="after"),
    ],
)
def test_associations_cannot_guess_record_ids_from_untyped_values(binding):
    with pytest.raises(ValueError, match="exact record reference"):
        flow.AssociationMapping("quote.deal", binding)


def test_exact_record_association_checks_related_object(payload):
    payload["references"].append(
        flow.Reference("fixed.deal", "record", "synthetic.fixed-deal", "deal").to_dict()
    )
    binding = flow.ValueBinding("reference", reference_id="fixed.deal").to_dict()
    node(payload, "action")["spec"]["associations"][0]["record"] = binding
    for case in ("match", "retry"):
        scenario(payload, case)["expected_associations"][0]["record"] = binding
    flow.Blueprint.from_dict(payload)
    reference(payload, "fixed.deal")["parent_id"] = "quote"
    with pytest.raises(ValueError, match="different related object"):
        flow.Blueprint.from_dict(payload)


def test_duplicate_exact_reference_cannot_hide_conflicting_field_mappings(payload):
    alias = deepcopy(reference(payload, "quote.status"))
    alias["id"] = "quote.status-alias"
    payload["references"].append(alias)
    with pytest.raises(ValueError, match="alias"):
        flow.Blueprint.from_dict(payload)


@pytest.mark.parametrize("collection", ["resources", "references", "scenarios"])
def test_duplicate_logical_id_rejected(payload, collection):
    payload[collection].append(deepcopy(payload[collection][0]))
    with pytest.raises(ValueError, match="unique identities"):
        flow.Blueprint.from_dict(payload)


@pytest.mark.parametrize("case", ["no_match", "match", "retry"])
def test_required_scenario_coverage_cannot_be_skipped(payload, case):
    scenario(payload, case)["required"] = False
    with pytest.raises(ValueError, match="requires no_match, match and retry"):
        flow.Blueprint.from_dict(payload)


def test_retry_requires_identical_event_and_one_created_record(payload):
    retry = scenario(payload, "retry")
    retry["events"][1]["id"] = "different-event"
    with pytest.raises(ValueError, match="identical event"):
        flow.Blueprint.from_dict(payload)
    retry["events"][1] = deepcopy(retry["events"][0])
    retry["expected_created_count"] = 2
    with pytest.raises(ValueError, match="one on match/retry"):
        flow.Blueprint.from_dict(payload)


def test_scenario_boolean_is_not_an_integer_count(payload):
    scenario(payload, "match")["expected_created_count"] = True
    with pytest.raises(ValueError, match="integer"):
        flow.Blueprint.from_dict(payload)


@pytest.mark.parametrize("collection", ["expected_fields", "expected_associations"])
def test_match_must_assert_all_created_fields_and_associations(payload, collection):
    scenario(payload, "match")[collection].pop()
    with pytest.raises(ValueError, match="assert every created"):
        flow.Blueprint.from_dict(payload)


@pytest.mark.parametrize("field", ["deal.stage", "deal.title"])
def test_scenarios_supply_watched_and_evaluated_fields(payload, field):
    del scenario(payload, "match")["events"][0]["after"][field]
    with pytest.raises(ValueError, match="scenario must supply"):
        flow.Blueprint.from_dict(payload)


def test_explicit_unsupported_artifact_can_be_inspected_but_not_accepted(payload):
    payload["unsupported"] = [
        flow.UnsupportedFinding(
            "native.delay", "Delay cannot be reproduced", "source.wait"
        ).to_dict()
    ]
    payload["scenarios"] = []
    blueprint = flow.Blueprint.from_dict(payload)
    assert blueprint.to_dict()["unsupported"] == payload["unsupported"]
    with pytest.raises(ValueError, match=r"unsupported semantics: native\.delay"):
        blueprint.require_supported()


def test_template_cannot_claim_source_mapping(payload):
    payload["references"].append(
        flow.Reference("source.deal", "object", "source.deal", scope="source").to_dict()
    )
    with pytest.raises(ValueError, match="template Blueprints"):
        flow.Blueprint.from_dict(payload)


def test_source_blueprint_requires_consistent_parent_mapping(payload):
    payload["origin"]["kind"] = "source"
    payload["references"] += [
        flow.Reference("source.deal", "object", "source.deal", scope="source").to_dict(),
        flow.Reference(
            "source.stage", "property", "stage", "source.deal", scope="source"
        ).to_dict(),
    ]
    payload["mappings"] = [flow.Mapping("source.stage", "deal.stage").to_dict()]
    with pytest.raises(ValueError, match="matching object mapping"):
        flow.Blueprint.from_dict(payload)
    payload["mappings"].append(flow.Mapping("source.deal", "deal").to_dict())
    assert flow.Blueprint.from_dict(payload).origin.kind == "source"


def add_planned_property(payload):
    reference(payload, "quote.status").update(binding="resource", key="new.status")
    payload["resources"].append(
        flow.Resource(
            "new.status",
            "property",
            {
                "object_ref": "quote",
                "name": "Status",
                "key": "status",
                "value_type": "text",
                "required": True,
            },
        ).to_dict()
    )


def test_planned_references_require_explicit_dependencies(payload):
    add_planned_property(payload)
    with pytest.raises(ValueError, match="explicit dependency"):
        flow.Blueprint.from_dict(payload)
    payload["resources"][0]["depends_on"] = ["new.status"]
    flow.Blueprint.from_dict(payload)
    payload["resources"][1]["depends_on"] = ["sales.quote"]
    with pytest.raises(ValueError, match="cycle"):
        flow.Blueprint.from_dict(payload)


def test_planned_property_and_reference_must_agree_on_object(payload):
    add_planned_property(payload)
    payload["resources"][1]["spec"]["object_ref"] = "deal"
    with pytest.raises(ValueError, match="resource object"):
        flow.Blueprint.from_dict(payload)


def test_dependencies_cannot_resolve_outside_blueprint(payload):
    payload["resources"][0]["depends_on"] = ["another-installation"]
    with pytest.raises(ValueError, match="resolve within"):
        flow.Blueprint.from_dict(payload)


def test_source_snapshot_retains_native_unsupported_graph_as_evidence():
    native_config = {"timeout": 60, "branches": ["yes", "later"]}
    snapshot = flow.SourceSnapshot(
        identity=flow.ArtifactIdentity(
            "synthetic/native-flow", "1", flow.artifact_digest(native_config)
        ),
        endpoint="synthetic-source",
        workspace="synthetic-workspace",
        nodes=(flow.SourceNode("wait", "unsupported", "native.delay", native_config),),
        edges=(flow.SourceEdge("wait", "wait", "retry"),),
        references=(flow.Reference("source.deal", "object", "deals", scope="source"),),
        unsupported=(
            flow.UnsupportedFinding(
                "native.delay", "Delay requires an explicit implementation", "wait"
            ),
        ),
    )
    original = snapshot.to_dict()
    native_config["branches"].clear()
    snapshot.nodes[0].configuration.clear()
    assert snapshot.to_dict() == original
    assert flow.SourceSnapshot.from_dict(original) == snapshot
    assert snapshot.digest == flow.artifact_digest(original)
    with pytest.raises(ValueError, match="explicit findings"):
        replace(snapshot, unsupported=())
    with pytest.raises(ValueError, match="captured nodes"):
        replace(snapshot, edges=(flow.SourceEdge("wait", "missing", "next"),))
    with pytest.raises(ValueError, match="captured node"):
        replace(snapshot, unsupported=(flow.UnsupportedFinding("missing", "Unknown", "missing"),))
    with pytest.raises(ValueError, match="source-scoped"):
        replace(snapshot, references=(flow.Reference("target", "object", "deals"),))

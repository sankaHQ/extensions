# SPDX-License-Identifier: Apache-2.0
"""Contract checks for declarative business requests and mandatory safety policies."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from sanka_extensions import code, flow


def test_create_is_unresolved_and_independent_of_runtime_or_templates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    definition = flow.create(type="acme/crm", parameters={"language": "ja"})
    assert definition.type == "acme/crm"
    assert definition.parameters == {"language": "ja"}
    assert list(tmp_path.iterdir()) == []
    assert not hasattr(definition, "apply")
    assert not hasattr(definition, "activate")


def test_definition_survives_json_roundtrip_and_preserves_parameter_types():
    definition = flow.create(
        type="billing",
        parameters={"enabled": True, "count": 3, "rate": 1.5, "fields": ["due-date", None]},
    )
    wire = json.loads(json.dumps(flow.encode_definition(definition)))
    assert flow.decode_definition(wire) == definition


def test_flow_definition_is_not_accepted_as_a_code_execution_request():
    payload = flow.encode_definition(flow.create(type="crm"))
    with pytest.raises(ValueError):
        code.decode_request(payload)


def test_input_and_returned_copies_cannot_change_a_definition():
    parameters = {"nested": {"names": ["customer"]}}
    definition = flow.create(type="crm", parameters=parameters)
    original = flow.encode_definition(definition)
    parameters["nested"]["names"].append("invoice")
    definition.parameters["nested"]["names"].append("payment")
    output = flow.encode_definition(definition)
    output["parameters"]["nested"]["names"].append("order")
    output["policies"]["reapply"]["user_changes"] = "overwrite"
    assert flow.encode_definition(definition) == original
    with pytest.raises(FrozenInstanceError):
        definition.type = "billing"


@pytest.mark.parametrize("value", ["", "CRM", " crm", "crm ", "../crm", "a/b/c", 1, None])
def test_invalid_type_is_rejected(value):
    with pytest.raises(ValueError, match="type must"):
        flow.create(type=value)


@pytest.mark.parametrize(
    "value", [{"bad": float("nan")}, {"bad": float("inf")}, {1: "bad"}, {"bad": object()}, []]
)
def test_non_json_parameters_are_rejected(value):
    with pytest.raises(ValueError):
        flow.create(type="crm", parameters=value)


def test_cyclic_parameters_are_rejected():
    parameters = {}
    parameters["self"] = parameters
    with pytest.raises(ValueError, match="cycles"):
        flow.create(type="crm", parameters=parameters)


@pytest.mark.parametrize(
    "policy,field,value",
    [
        ("reapply", "user_changes", "overwrite"),
        ("reapply", "conflicts", "ignore"),
        ("reapply", "ownership", "matching_name"),
        ("reapply", "removals", "all_missing"),
        ("lifecycle", "stages", ["construct", "activate"]),
        ("lifecycle", "new_automations", "enabled"),
        ("lifecycle", "activation", "automatic"),
    ],
)
def test_decoding_rejects_weakened_reapply_and_activation_contracts(policy, field, value):
    payload = flow.encode_definition(flow.create(type="crm"))
    payload["policies"][policy][field] = value
    with pytest.raises(ValueError, match="policies must"):
        flow.decode_definition(payload)


@pytest.mark.parametrize("change", ["version", "missing", "extra", "extra_policy"])
def test_decoding_rejects_unsupported_and_ambiguous_payloads(change):
    payload = flow.encode_definition(flow.create(type="crm"))
    if change == "version":
        payload["schema_version"] = "sanka-flow-definition/v2"
    elif change == "missing":
        del payload["policies"]
    elif change == "extra":
        payload["activate"] = True
    else:
        payload["policies"]["activate_without_verification"] = True
    with pytest.raises(ValueError):
        flow.decode_definition(payload)

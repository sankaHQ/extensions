# SPDX-License-Identifier: Apache-2.0
"""Declarative Sanka Flow artifacts; this module never executes a workflow."""

from sanka_extensions.flow._wire import artifact_digest
from sanka_extensions.flow.blueprint import BLUEPRINT_SCHEMA_VERSION, Blueprint, Resource
from sanka_extensions.flow.definition import (
    SCHEMA_VERSION,
    FlowDefinition,
    create,
    decode_definition,
    encode_definition,
)
from sanka_extensions.flow.graph import (
    Action,
    AssociationMapping,
    Condition,
    FieldMapping,
    RecordIdentity,
    Trigger,
    ValueBinding,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
)
from sanka_extensions.flow.identity import (
    ArtifactIdentity,
    BlueprintOrigin,
    Mapping,
    Reference,
    UnsupportedFinding,
)
from sanka_extensions.flow.scenario import Scenario, ScenarioEvent
from sanka_extensions.flow.source import (
    SOURCE_SCHEMA_VERSION,
    SourceEdge,
    SourceNode,
    SourceSnapshot,
)

__all__ = [
    "BLUEPRINT_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "SOURCE_SCHEMA_VERSION",
    "Action",
    "ArtifactIdentity",
    "AssociationMapping",
    "Blueprint",
    "BlueprintOrigin",
    "Condition",
    "FieldMapping",
    "FlowDefinition",
    "Mapping",
    "RecordIdentity",
    "Reference",
    "Resource",
    "Scenario",
    "ScenarioEvent",
    "SourceEdge",
    "SourceNode",
    "SourceSnapshot",
    "Trigger",
    "UnsupportedFinding",
    "ValueBinding",
    "WorkflowEdge",
    "WorkflowGraph",
    "WorkflowNode",
    "artifact_digest",
    "create",
    "decode_definition",
    "encode_definition",
]

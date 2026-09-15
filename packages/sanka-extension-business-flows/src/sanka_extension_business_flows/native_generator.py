# SPDX-License-Identifier: Apache-2.0
"""Candidate side-effect-free generators for the 27 closed native recipes.

SDK a7 publication precedes adoption into the released entrypoint and manifest.
The host remains responsible for reference authorization, native translation,
construction, execution, verification and explicit activation.
"""

from __future__ import annotations

from typing import Any, Final

from sanka_extension_business_flows.catalog import recipe
from sanka_extension_business_flows.native_parameters import resolve_native_parameters
from sanka_extension_business_flows.native_recipes import native_recipe, recipe_for_selector
from sanka_extensions.flow import (
    ArtifactIdentity,
    Blueprint,
    BlueprintOrigin,
    BlueprintRequest,
    BlueprintResponse,
    FlowCapability,
    FlowDefinition,
    NativeAssignedTaskWorkflow,
    NativeBusinessProcessWorkflow,
    NativeRecordConversionWorkflow,
    NativeSourceApprovalWorkflow,
    Resource,
    artifact_digest,
)
from sanka_extensions.flow.native_profiles import NativeWorkflow, decode_native_workflow

EXTENSION_ID = "sanka/business-flows"
# This is a candidate identity, not a claim that this version is published.
CANDIDATE_EXTENSION_VERSION = "0.1.0a2"
OUTPUT_SCHEMA: Final = "sanka-flow-blueprint/v5"


def native_capability(recipe_id: str) -> FlowCapability:
    selected = native_recipe(recipe_id)
    metadata = recipe(recipe_id)
    identity = ArtifactIdentity(
        selected.selector,
        str(metadata["version"]),
        artifact_digest(
            {
                "schema_version": "sanka-business-native-recipe/v1",
                "recipe": metadata,
                "binding": selected.identity_fields(),
            }
        ),
    )
    return FlowCapability(selected.selector, (), (), identity, OUTPUT_SCHEMA)


def native_profile(recipe_id: str, parameters: dict[str, Any]) -> NativeWorkflow:
    """Resolve initial manual/AI input into settings visible in the reviewed plan.

    Managed refreshes compare this desired profile with the saved template
    baseline and observed native state; they must not re-normalize user edits.
    """
    selected = native_recipe(recipe_id)
    settings = resolve_native_parameters(recipe_id, parameters)
    match selected.family:
        case "conversion":
            return NativeRecordConversionWorkflow.from_configuration(
                selected.workflow_id, {"conversion": selected.variant, "settings": settings}
            )
        case "approval":
            return NativeSourceApprovalWorkflow.from_configuration(
                selected.workflow_id,
                {"subject": selected.variant, "approver_ids": settings["approver_ids"]},
            )
        case "task":
            return NativeAssignedTaskWorkflow.from_configuration(
                selected.workflow_id, {"task": selected.variant, "settings": settings}
            )
        case "process":
            return NativeBusinessProcessWorkflow.from_configuration(
                selected.workflow_id, {"process": selected.variant, "settings": settings}
            )
    raise ValueError("Unsupported business recipe family")


def native_definition(recipe_id: str, parameters: dict[str, Any]) -> FlowDefinition:
    profile = native_profile(recipe_id, parameters)
    return FlowDefinition(
        type=native_recipe(recipe_id).selector,
        parameters={"native_workflow": profile.to_dict()},
    )


def generate_native(request: BlueprintRequest) -> BlueprintResponse:
    selected = recipe_for_selector(request.definition.type)
    supported = native_capability(selected.recipe_id)
    supported.validate_request(request)
    if (
        request.extension.id != EXTENSION_ID
        or request.extension.revision != CANDIDATE_EXTENSION_VERSION
    ):
        raise ValueError("Request must identify this exact candidate extension version")
    supplied = decode_native_workflow(request.definition.parameters["native_workflow"])
    # Rebuild from this recipe's exact binding, not caller-selected roles or
    # another valid SDK profile. Raw protocol callers cannot bypass catalog
    # ranges, canonical settings or stable recipe identities.
    configuration = supplied.configuration
    settings = (
        {"approver_ids": configuration["approver_ids"]}
        if isinstance(supplied, NativeSourceApprovalWorkflow)
        else configuration.get("settings")
    )
    if type(settings) is not dict:
        raise ValueError("Request must contain the selected native recipe settings")
    profile = native_profile(selected.recipe_id, settings)
    if artifact_digest(profile.to_dict()) != artifact_digest(supplied.to_dict()):
        raise ValueError("Request differs from the selected recipe's canonical profile")
    blueprint = Blueprint(
        id=selected.workflow_id,
        revision=supported.template.revision,
        origin=BlueprintOrigin("template", supported.template),
        extension=request.extension,
        resources=(Resource(profile.id, "workflow", profile.to_dict()),),
        parameters=request.blueprint_parameters,
        schema_version=OUTPUT_SCHEMA,
    )
    return BlueprintResponse.success(request, blueprint)

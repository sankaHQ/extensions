# SPDX-License-Identifier: Apache-2.0
"""Select the typed order/billing profile; the host owns all provider execution."""

from __future__ import annotations

from typing import Any

from sanka_extension_business_flows.catalog import recipe, resolve_parameters
from sanka_extensions.flow import (
    ArtifactIdentity,
    Blueprint,
    BlueprintOrigin,
    BlueprintRequest,
    BlueprintResponse,
    FlowCapability,
    FlowDefinition,
    NativeOrderBillingWorkflow,
    Resource,
    artifact_digest,
)

EXTENSION_ID = "sanka/business-flows"
EXTENSION_VERSION = "0.1.0a1"
RECIPE_ID = "billing.hubspot-deal-invoices"
SELECTOR = "sanka/hubspot-deal-invoices"
WORKFLOW_ID = "hubspot-order-billing"


def capability() -> FlowCapability:
    metadata = recipe(RECIPE_ID)
    identity = ArtifactIdentity(
        SELECTOR,
        str(metadata["version"]),
        artifact_digest({"schema_version": "sanka-business-recipe/v1", "recipe": metadata}),
    )
    return FlowCapability(SELECTOR, (), (), identity, "sanka-flow-blueprint/v3")


def definition(parameters: dict[str, Any], *, mapping: ArtifactIdentity) -> FlowDefinition:
    """Resolve UI settings with a host-validated immutable order mapping snapshot."""
    settings = resolve_parameters(RECIPE_ID, parameters)
    if mapping.id != settings.pop("mapping_template_id"):
        raise ValueError("Resolved mapping differs from the selected mapping")
    settings["mapping"] = mapping.to_dict()
    profile = NativeOrderBillingWorkflow.from_configuration(WORKFLOW_ID, settings)
    return FlowDefinition(type=SELECTOR, parameters={"native_configuration": profile.configuration})


def generate(request: BlueprintRequest) -> BlueprintResponse:
    supported = capability()
    supported.validate_request(request)
    if request.extension.id != EXTENSION_ID or request.extension.revision != EXTENSION_VERSION:
        raise ValueError("Request must identify this exact installed extension version")
    profile = NativeOrderBillingWorkflow.from_configuration(
        WORKFLOW_ID, request.definition.parameters["native_configuration"]
    )
    # Enforce recipe-level ranges even for callers constructing SDK requests
    # directly, without passing through the manual/AI configuration helper.
    settings = profile.configuration
    settings.pop("mapping")
    settings["mapping_template_id"] = profile.mapping.id
    resolve_parameters(RECIPE_ID, settings)
    blueprint = Blueprint(
        id=WORKFLOW_ID,
        revision=supported.template.revision,
        origin=BlueprintOrigin("template", supported.template),
        extension=request.extension,
        resources=(Resource(WORKFLOW_ID, "workflow", profile.to_dict()),),
        parameters=request.blueprint_parameters,
        schema_version="sanka-flow-blueprint/v3",
    )
    return BlueprintResponse.success(request, blueprint)

# SPDX-License-Identifier: Apache-2.0
"""Static capability metadata and immutable bundled template identity."""

from __future__ import annotations

import json
from importlib.resources import files

from sanka_extensions.flow import (
    ArtifactIdentity,
    FlowCapability,
    ReferenceRequirement,
    ValueRequirement,
    artifact_digest,
)

EXTENSION_ID = "sanka/sales-quote"
VERSION = "0.1.0a1"
WORKFLOW_ID = "sales.quote"
CAPABILITY = FlowCapability(
    type=EXTENSION_ID,
    references=(
        ReferenceRequirement("deal", "object"),
        ReferenceRequirement("deal.stage", "property", "deal"),
        ReferenceRequirement("deal.title", "property", "deal"),
        ReferenceRequirement("quote", "object"),
        ReferenceRequirement("quote.status", "property", "quote"),
        ReferenceRequirement("quote.title", "property", "quote"),
        ReferenceRequirement("quote.deal", "relationship", "quote", "deal"),
    ),
    values=(
        ValueRequirement("quote_stage", ("string",)),
        ValueRequirement("other_stage", ("string",)),
        ValueRequirement("draft_status", ("string",)),
    ),
)


def template_identity() -> ArtifactIdentity:
    template = json.loads(files(__package__).joinpath("template.json").read_text(encoding="utf-8"))
    return ArtifactIdentity(EXTENSION_ID, template["revision"], artifact_digest(template))

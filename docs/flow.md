# Sanka Flow contracts

## Source API and execution status

Use `sanka_extensions.flow` for business reconstruction. `Blueprint` may name the
resolved configuration artifact inside Flow; it is not a separate SDK namespace.

```python
from sanka_extensions import flow

crm = flow.create(type="crm", parameters={"language": "ja"})
request = flow.encode_definition(crm)
restored = flow.decode_definition(request)
```

`create` constructs an immutable, unresolved `FlowDefinition`. It performs no
extension discovery, template loading, network access, workspace mutation or
activation. Types are lowercase kebab-case selectors, optionally qualified as
`publisher/name`. The SDK does not bundle CRM or billing knowledge or assert that
an extension with that type is installed. Parameters contain JSON configuration;
credentials belong in separately configured systems managed by the runtime.

This source change adds the definition contract and its validation tests. It does
not add a Flow marketplace package, executable protocol/manifest kind, CLI command
or Setup Wizard integration. Published code/system contracts remain unchanged.
Runtimes must reject unsupported Flow execution rather than treating a definition
as a code-migration request or ignoring its policies.

## Versioned definition

The exact `sanka-flow-definition/v1` payload has four fields:

```json
{
  "schema_version": "sanka-flow-definition/v1",
  "type": "crm",
  "parameters": {},
  "policies": {
    "reapply": {
      "user_changes": "preserve",
      "conflicts": "require_resolution",
      "ownership": "installation",
      "removals": "explicit_plan"
    },
    "lifecycle": {
      "stages": ["construct", "verify", "activate"],
      "new_automations": "disabled_until_activation",
      "activation": "explicit_verified_revision"
    }
  }
}
```

Missing/extra fields, unsupported versions and altered policies are rejected.
Parameters must be finite JSON values with string object keys. Input dictionaries
and returned copies cannot mutate an existing definition. These requirements are
fixed in this schema; changing them requires an explicitly versioned contract.
Definition validation establishes the request shape, not runtime enforcement.

## Reapplication and user changes

The executing runtime must keep an installation identity and stable logical IDs
mapped to exact target IDs. Store the last applied configuration/revision and
compare it with current and desired values when planning a subsequent update.

Preserve user edits where the template has not changed. Report a conflict when
both changed the same value; require an explicit resolution and a new reviewed
plan. Existing matching names/slugs do not establish ownership. Adoption is a
reviewed operation, and removal must be an explicit plan operation limited to
installation-owned resources with dependencies considered.

Plans pin the resolved extension identity, template version/digest, target and
observed revisions. Recheck these preconditions before applying. Persist operation
identities and outcomes for recovery: a repeated request must not duplicate
resources, and the same idempotency key with different content must fail.

## Construction, verification and activation

Construction applies a reviewed plan and leaves new automations disabled.
Existing active versions remain available while replacements are staged. A target
that cannot stage or version changes must expose that limitation and a supported,
explicit cutover path in the plan.

Verification reads back the constructed configuration and runs required scenarios
using isolated test records without live external effects. Evidence binds the
installation, reviewed plan and exact constructed revision. Skipped required
checks are not successful verification.

Activation is a separate explicit request. The runtime accepts only successful,
complete verification of the same installation, plan and current constructed
revision. A changed plan or target invalidates prior evidence. Store activation
outcomes so retries recover rather than repeat external effects. Neither
`flow.create` nor construction success means a workflow is active.

## Repository boundaries

`extensions` owns this SDK and reusable business definitions/transformations.
`sanka` owns shared OSS loading, planning, execution, verification and recovery.
SDK and extension code must not import that runtime. `sanka-api` retains private
cloud authentication, workspace services, hosted SaaS adapters and jobs;
`sanka-react` owns review and operation screens. Cloud integration should reuse
existing domain services, including the current app-builder apply boundary.

Implement and verify runtime enforcement before advertising a runnable Flow
extension. SDK publication, runtime upgrades and cloud deployment remain separate
release operations.

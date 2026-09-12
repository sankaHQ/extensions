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
credentials belong in separately configured data endpoints managed by the runtime.

The SDK also describes captured sources, resolved Blueprints and required scenarios.
These contracts do not add a Flow marketplace package, executable protocol/manifest
kind, CLI command or Setup Wizard integration. Published code/data contracts remain unchanged.
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

## Captured sources and resolved Blueprints

Source imports and reusable templates converge on the same `Blueprint`:

```text
captured source -> SourceSnapshot -> extension normalization --+
                                                             +-> Blueprint
FlowDefinition -> extension + versioned template ------------+
```

`SourceSnapshot` uses `sanka-flow-source/v1`. Its `identity` pins the original
captured artifact; `endpoint` and `workspace` identify its source context without
credentials. `nodes`, `edges` and source-scoped `references` preserve native
configuration as evidence. Native cycles and branch labels can be captured, but
are not executable Flow semantics. An unsupported node requires an explicit
`UnsupportedFinding`; findings reference captured nodes or the whole snapshot.
`snapshot.digest` hashes the complete normalized snapshot. It differs from
`snapshot.identity.digest`, which hashes the original capture.

`Blueprint` uses `sanka-flow-blueprint/v1` and has these exact fields:

| Field | Meaning |
| --- | --- |
| `id`, `revision` | Stable desired configuration identity and revision |
| `origin` | `source` or `template`, plus original artifact ID, revision and digest |
| `extension` | Exact generating extension artifact ID, revision and digest |
| `parameters` | Resolved JSON configuration, without credentials |
| `policies` | The unchanged definition policies above |
| `resources` | Logical object, property and workflow specifications with dependencies |
| `references` | Exact source/target keys scoped by object, never display-name adoption |
| `mappings` | Explicit same-kind source-to-target references for imported sources |
| `scenarios` | Required events and expected created records, fields and associations |
| `unsupported` | Blocking findings; there is no warning or ignore mode |

Use `Blueprint.to_dict()` / `Blueprint.from_dict()` and the equivalent
`SourceSnapshot` methods for JSON boundaries. `ArtifactIdentity.digest` requires
`sha256:<64 lowercase hex characters>`. `artifact_digest(value)` hashes canonical
finite UTF-8 JSON with sorted object keys, without ASCII escaping. A Blueprint's `digest` includes its complete
serialized configuration, scenarios, policies and provenance. Lists that represent
sets are sorted by logical identity; event ordering remains significant. Input
mutation and modification of returned dictionaries cannot change either artifact.

An empty desired resource set is valid. It lets a runtime plan omission-only
preservation or an explicitly requested uninstall. Empty desired configuration
alone never authorizes deletion; only reviewed removal operations do that.

Each `Reference` has a logical `id`, `kind`, exact `key`, `scope`, `binding`,
`parent_id` and `related_object_id`. Properties, records and relationships require
an object parent in the same scope; relationships also require their related
object. Other kinds have no related object. `binding="resource"` points to a
planned object or property resource; all consumers must declare that dependency.
Existing references cannot claim children of an unconstructed object. Duplicate
logical identities, aliases of the same scoped key, dangling references, dependency
cycles and inconsistent parent mappings are rejected. A template cannot claim
source mappings. Host adapters must resolve exact keys in the pinned target and
check the actual object/property/relationship types before construction.

Object resources declare `name` and `slug`; property resources declare
`object_ref`, `name`, `key`, `value_type` (`text`, `number`, `boolean`, `date` or
`datetime`) and `required`. These declarations do not adopt same-name objects or
authorize the host to coerce existing values. A host that cannot preserve the
declared behavior must report an unsupported finding.

## First supported workflow and scenarios

The first `WorkflowGraph` supports exactly three typed nodes and two edges:

1. A `record.updated` trigger for one object, watching one or more properties.
2. An `equals` condition reached unconditionally from the trigger.
3. A `record.create` action reached only when the condition is true.

The trigger matches only when at least one watched field differs between the
event's `before` and `after` values. Equality compares canonical finite JSON,
without truthiness, string conversion, case folding or numeric coercion: `true`,
`1`, `1.0` and `"1"` remain distinct. A false condition ends without an action.
Unknown operations, branch edges, delays and custom code are rejected. This is
a deliberately bounded executable meaning; native semantics that differ must
remain explicit unsupported findings until implemented.

`ValueBinding` selects a literal JSON value, the event record ID, a named event
field in the `before` or `after` phase, or an exact record reference. Field mappings
name target properties. Associations identify a relationship and bind the event
record or an exact record reference of its related object; untyped field values
cannot serve as association record IDs.

`RecordIdentity` fixes one bound target per installation, workflow, action and
event record. Repeated delivery and leaving/reentering the matching stage preserve
the same target and its user edits. If that bound target disappears, raise a
conflict; do not recreate it or match another record by name. The runtime must
persist and enforce this identity using native action services.

Every supported workflow requires `Scenario` coverage for `no_match`, `match` and
`retry`. A scenario has a logical ID, workflow ID, ordered events, required flag,
expected created count, and expected field/association mappings. Each event has
an ID, record ID and before/after dictionaries keyed by property reference.
No-match supplies one event and expects zero created records. Match supplies one
event and expects one. Retry repeats the identical event at least twice and still
expects one. Match and retry assert every action field and association. Watched
and evaluated event fields must be supplied; missing values are not implicit nulls.

The SDK validates these assertions and their references, but does not run them or
prove the expected outcome. Runtime verification must execute the target's native
workflow implementation in isolation and compare actual readback to these
assertions. Scenario names, a host success flag, a test fake or SDK round-tripping
alone do not establish business equivalence. `blueprint.require_supported()`
rejects any blocking findings; runtime planning must enforce the same check.

The [synthetic sales fixture](../packages/sanka-extension-sdk/tests/fixtures/synthetic_sales_quote_blueprint.json)
shows a Deal stage change to `Quote` producing one linked draft Quote. Its object
keys, record IDs and artifact identities are synthetic. It demonstrates the
contract and its three scenario cases, without claiming a real source import,
installed template, Sanka cloud field mapping, quote pricing or external delivery.

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

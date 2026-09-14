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
These contracts do not add a Flow marketplace package, CLI command or Setup Wizard
integration. SDK a4 adds the generator protocol described below; native execution
and manifest admission remain runtime responsibilities. Published code/data contracts remain unchanged.
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


## Creation triggers and optional conditions (v2)

The published `0.1.0a3` SDK adds `sanka-flow-blueprint/v2`. Existing v1
artifacts and their canonical digests remain unchanged. V1 keeps its update,
condition, action shape; v2 artifacts must not be passed to a v1-only compiler.
V2 workflow specs carry `schema_version: sanka-flow-graph/v2` and support exactly:

- one `record.created` trigger with empty `changed_fields`, or one
  `record.updated` trigger watching at least one exact property reference;
- an optional `equals` condition, with false meaning skip;
- one `record.create` action with explicit fields, associations and record identity.

A direct graph has one `always` edge from trigger to action. A conditional graph
has `always` from trigger to condition and `true` from condition to action.
Branches, cycles, additional actions and unknown operations are rejected.
Creation triggers cannot read before-state fields or specify watched changes.

V2 Blueprints include a canonical `required_capabilities` list derived from the
resource kinds, graph versions, operations, associations and record-identity
policy. A caller cannot remove a capability from the serialized artifact. The
host must call `blueprint.require_supported(capabilities=frozenset(...))` with
its independently supported features **before planning or mutation**. Omitting
capabilities rejects v2. This check does not establish that a claimed capability
works: the native compiler and executor require their own acceptance evidence.
The shared runtime's structural Blueprint port expects the host to perform this
validation and bind the capability observation into the plan.

A creation scenario event includes `operation: record.created`, empty `before`,
and explicit `after` values. An omitted operation means `record.updated`,
preserving existing event serialization. Match and retry events must agree with
the trigger operation. All three required cases remain mandatory. For an
unconditional creation workflow, no-match uses an update event as a negative
control; it cannot claim that a creation event produces no record. Retry repeats
the identical event and still expects exactly one created record. Verification
must use native execution and record readback; parsing a fixture is not proof.

The synthetic creation fixture in
`packages/sanka-extension-sdk/tests/fixtures/synthetic_sales_created_estimate_blueprint.json`
shows the two-node Deal-created → Estimate pattern. Its references and provenance
are synthetic, not an installed extension or live native mapping. The existing
native Workflows compiler is the intended hosted target; it must retain normal
Save/reload and inactive-draft behavior. No hosted credentials, provider clients,
automation executor or separate user-facing installation/history UI belongs in
this SDK. Publish the SDK before advancing runtime pins or wiring an executable
extension through that compiler.

## Isolated generator protocol (SDK a4 candidate)

`sanka_extensions.flow.protocol` defines `sanka-flow-extension/v1`: one typed
`BlueprintRequest` on stdin and one `BlueprintResponse` on stdout. Its sole operation
is `blueprint`. Apply, activation, source import and code migration are rejected.
The SDK provides message types and validation; it does not start a process or
perform native writes. `flow.create` retains its existing side-effect-free behavior.

A static `FlowCapability` declares the selector, exact template artifact
ID/revision/digest, requested Blueprint schema (v1 or v2), reference roles and
scalar value types. Hosts validate the request against this verified manifest before
invoking extension code. Requests contain that template identity, the verified
extension identity, exact existing target references, explicit values, definition
parameters, and a target ID/revision/capability snapshot. All these inputs are
included in the canonical request digest. Capabilities must come from independent
host validation, not from the extension or the client requesting generation.

After decoding a response, hosts must call `response.validate_for(request)`.
Constructing `BlueprintResponse.success` calls the same check. It rejects a changed
request ID/digest, extension or template identity, schema, reference set, target,
values or definition parameters. It also requires the target's explicit capabilities
to cover the Blueprint and rejects blocking unsupported findings for **both**
Blueprint versions. An error response carries a typed failure and no Blueprint.

Messages are finite UTF-8 JSON objects bounded to 4 MiB. Duplicate keys, unknown or
missing fields, invalid Unicode, nonfinite numbers, mixed success/error bodies and
unknown versions are rejected. Selected references must be target-scoped existing
keys; this template protocol does not create reference inputs or import a source.
The host still validates actual native object/property/relationship types and
rechecks target revisions before planning and mutation.

Successful generation proves only that an artifact satisfies this boundary. It
does not prove the generator is trustworthy, the target supports claimed behavior,
or scenarios have executed. Runtime isolation, verified wheel and environment
provenance, durable record identity and native scenario readback remain mandatory.
Process/environment isolation must not be described as an OS or network sandbox.

## Native order-import and billing profile (candidate, not released)

Blueprint v3 adds `NativeOrderBillingWorkflow`, a bounded native capability
profile for interval scheduling, complete HubSpot deal import into orders, and
creation of draft invoices from that import's output. The declaration contains
no credentials, provider clients, import implementation or invoice executor.
Versioned business template packages select this profile and its settings; the
private host resolves endpoints and saved mappings and executes the native actions.

The profile fixes stable schedule/import/invoice node identities and rejects
partial-import continuation, changing the import target to invoices, selecting
all orders, overwriting existing invoices, or constructing active workflows.
The mapping carries its exact ID, revision and digest. Hosts must resolve these
against the selected workspace; syntactically valid IDs do not establish access.

The isolated generator protocol admits v3 without changing existing v1/v2 wire
forms. A v3 request uses exactly `definition.parameters.native_configuration`,
validated by `NativeOrderBillingWorkflow.from_configuration`, with empty portable
references and scalar values. Output must contain exactly one native workflow,
with identical executable configuration as well as unchanged request metadata,
target, extension and template identity. Every required native capability is
derived from the profile and checked against the independent host observation.

This candidate enables definition validation and planning of inactive construction.
It supplies **no native scenario verification or activation contract**. V3 forbids
portable graph scenarios: those describe a single record event creating zero or
one record and cannot prove scheduled imports, batch billing or retries. Shared
runtime verify/activate must reject v3 until the native scenario contract and
native execution adapter are reviewed and implemented. A capability declaration
is not evidence that provider execution works.

SDK source approval/publication precedes runtime SDK synchronization or consumer
pins. This change does not publish a package, upgrade the hosted API, migrate the
28 Studio recipes or modify any existing workflow. Remaining native recipe
profiles require typed contracts; arbitrary action dictionaries are not accepted.

The SDK candidate is version `0.1.0a5`, built and checked separately under
`release/sdk-candidate`. The workspace-only UV override exercises existing code
against the candidate; published extension package requirements remain pinned to
`0.1.0a4`. Marketplace builds retrieve that original SDK wheel from its immutable
SDK release and verify its exact size and SHA-256. Existing manifests and wheel
URLs/hashes are unchanged. Marketplace publication does not publish the new SDK;
SDK a5 requires its own reviewed publication before any consumer pin is advanced.

## Business recipe package candidate

`packages/sanka-extension-business-flows` owns the initial nine-category,
28-recipe catalog, including stable IDs, English/Japanese labels, typed fields,
defaults and range validation. Its initial catalog was extracted from the maintained
Studio catalog at API commit `ae1e2155d1f4381213c6c49f26c836cad7241457`,
`app/model/domain/workflows/template_catalog.py`; it introduces no business-type step.
Future consumers should use these package settings for manual forms and AI proposals.
This source change does not switch the hosted Studio consumer.

Only `billing.hubspot-deal-invoices` currently generates a Blueprint in this package.
It maps to selector `sanka/hubspot-deal-invoices` and the SDK's typed native order/billing
profile. The remaining 27 catalog entries report `definition_only`; that field
describes package generation support, not the availability of existing hosted recipes.

The host resolves data endpoints and immutable mapping snapshots in its workspace.
The isolated generator cannot read providers, authenticate endpoints or execute native
actions. Native verification and activation remain unsupported. See the
[package README](../packages/sanka-extension-business-flows/README.md) for the protocol.

`make build-business-flows` verifies the wheel, separate marketplace manifest and
published SDK dependency hashes. `--update-manifest` on its build script is only
for intentional candidate changes before publication. After publication, changed
bytes require a new package version and reviewed catalog; do not overwrite assets.
The dispatch-only `publish-business-flows.yml` requires the exact reviewed
`business-flows-v0.1.0a1` tag. Publish this package before the companion CLI 0.2.13
release; the CLI publisher verifies its real package through public artifact URLs.
This separate catalog does not change the default Data/Code marketplace.

## Native billing verification contract (SDK a6 candidate)

Blueprint `sanka-flow-blueprint/v4` adds a typed verification contract to the
existing `NativeOrderBillingWorkflow`. V1/v2/v3 serialization and semantics remain
unchanged. V3 still contains no scenarios and remains ineligible for shared
verification/activation. This SDK candidate declares checks; it does not execute
providers, prove native behavior or activate workflows.

A v4 workflow requires `flow.native.order-billing-verification/v1` in addition
to its existing native capabilities. `NativeBillingScenario` requires every case
exactly once: complete, scoped complete, empty, partial, failed, cancelled,
committed-invoice retry, import-handoff retry, repeated schedule, overlapping runs,
and a complete import of at least 2,001 source records. Each fixture includes a
billed existing Order, an unbilled existing Order, and a newly imported Order.
The scoped-complete case leaves an existing unbilled Order outside the nonempty
import. Incomplete cases import both new and existing unbilled Orders but must
create no invoices. These negative controls prevent a verifier from passing
merely because every tested Order was already billed or the import was empty.

Deliveries distinguish a unique attempt ID from the durable run ID. Retries use
the same run and pinned UTC clock; a later scheduled run uses a new run ID exactly
one configured interval later. Overlap uses distinct runs in one concurrent group.
The native harness must actually overlap their execution and provide admission/
timing readback; sequential deliveries with the same label are insufficient.
Failure injection is limited to the declared post-import and post-invoice-commit
boundaries, without scripts or executable hooks.

A scenario pins an immutable fixture manifest, the exact saved mapping artifact,
and the executable configuration digest. The host must independently admit that
fixture oracle before the plan is reviewed. The fixture contains synthetic source
records and independently specified expected Order and Invoice business fields;
expected values must not be calculated using the importer under test. A host
without an oracle for the selected mapping must report unsupported verification.
Mapping identity alone is not evidence that an arbitrary mapping is correct.

`NativeBillingFixtureManifest` lists disjoint, complete source-key membership and
immutable `NativeBillingFixturePageRef` artifacts. Pages contain at most 100
`NativeBillingFixtureRecord` values and 256 KiB of canonical JSON. Every page must
be loaded and hash-checked; every record and its seeded state must be compared.
This keeps the complete 2,001-record oracle out of repeated Blueprint, plan and
verification envelopes. Samples, host-reported counts and unchecked page hashes
cannot establish a pass.

Each fixture record declares a logical customer key, source data, expected Order
and draft Invoice fields, and optional initial Order/Invoice snapshots. Expectations
assert currency, totals, tax policy, complete line items and invoice dates. Money
and quantities use canonical decimal strings without float tolerance. The native
adapter binds source/customer/line keys to real isolated records, reads back actual
fields and associations, and preserves the complete initial invoice snapshot and
its identity across every attempt. The shared verifier must check due dates against
the pinned clock and configured due days, exact import membership, one invoice per
eligible Order, unchanged IDs on retry, and absence of outside billing effects.

V4 generator requests include `native_configuration` and `native_verification`
(the admitted scenario declarations and fixture references). Responses must preserve
both exactly. Full fixture record values are passed through the host's immutable
artifact port, not copied into definition parameters or executable action payloads.
The shared runtime owns page comparison, durable evidence, claim renewal for long
checks, revision-bound activation and recovery. SDK publication must precede
consumer dependency upgrades; the Business Flow package remains pinned to SDK a5
until its separately reviewed upgrade.

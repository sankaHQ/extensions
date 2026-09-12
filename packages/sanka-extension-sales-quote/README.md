# Sales draft Quote Flow extension

`sanka/sales-quote` generates a reusable Blueprint for one workflow: when a Deal
stage changes to the explicitly selected Quote stage, create one draft Quote and
link it to that Deal. The Deal title maps to the Quote title. The installed host
constructs the workflow inactive, verifies native execution in isolation, and
requires separate approval of the verified revision before activation.

This package implements the isolated `sanka-flow-extension/v1` `blueprint`
operation. It does not call APIs, read a database, send messages, calculate prices,
or execute business actions. Its only dependency is the Sanka Extension SDK.
There are no hosted-provider clients or imports of the AGPL Sanka runtime.

## Explicit input

Use `flow.create(type="sanka/sales-quote", parameters=...)`. Sales parameters have
exactly two fields:

```json
{
  "references": ["full typed Reference objects supplied by the host"],
  "values": {
    "quote_stage": "an exact selected stage value",
    "other_stage": "a different exact stage value for verification",
    "draft_status": "the exact native draft status value"
  }
}
```

The references array above is illustrative; every member must be a complete
`sanka_extensions.flow.Reference`, with an exact key and the following logical
roles. The host resolves those keys within one authenticated target and revision.
No display name establishes ownership or selects an existing resource.

| Role ID | Kind | Parent | Related object |
| --- | --- | --- | --- |
| `deal` | object | — | — |
| `deal.stage` | property | `deal` | — |
| `deal.title` | property | `deal` | — |
| `quote` | object | — | — |
| `quote.status` | property | `quote` | — |
| `quote.title` | property | `quote` | — |
| `quote.deal` | relationship | `quote` | `deal` |

All references must have `scope="target"` and `binding="existing"`. Stage and
status selections must be strings; the two stage values must differ. The host
must validate actual object/property types, stage membership, relationship
direction, draft semantics and any additional required Quote fields. Unsupported
native behavior becomes a plan blocker, without coercion or guessed defaults.

For Sanka's native Deal/Estimate model, the logical roles resolve to object keys
`deal` and `estimate`, Deal properties `stage` and `name`, and Estimate properties
`status` and `notes`. The logical `quote.title` means the mapped title text; native
Estimates have no title field, so that text is stored in `notes`, matching the
native Deal-to-Estimate conversion. The relationship key must be the exact
workspace-scoped association label UUID whose endpoints are `estimate` and `deal`.
It cannot be a guessed label or a UUID from another workspace. The stage strings
must come from the selected target configuration. These mappings do not establish
that the host can execute the template; native validation and scenario readback
are still required.

The protocol envelope repeats the selected references and values alongside the
FlowDefinition and exact target identity/revision. The generator rejects any
disagreement with the definition parameters. Its response echoes the exact
request digest and extension identity, and the Blueprint preserves the target,
references, values and definition parameters for review.

## Verification and repeat behavior

The generated Blueprint requires four isolated scenarios: a changed stage moving
away from Quote, an unchanged stage already at Quote, a matching transition,
and repeated delivery of the identical matching event. They assert zero/one
created records, draft status, title and the exact Deal association.

Record identity is stable per installation/workflow/action/Deal. A later stage
reentry preserves the same Quote and its user changes. If that bound Quote was
deleted, the host must report a conflict rather than issue another Quote.
Native acceptance must test those two cases in addition to the generated
same-event retry scenario. SDK and subprocess tests alone do not prove native
business equivalence.

## Process and release boundary

The executable reads one bounded UTF-8 JSON request from stdin and writes one
response to stdout. Success exits `0`; a handled request failure exits `1`;
malformed protocol input exits `2` without a response. Diagnostics use stderr.
Runtime discovery reads the static Flow manifest from `flow-marketplace.json`;
it never imports this module in the shared CLI process. The legacy Data/Code
catalog remains compatible with older clients.

Process separation and a minimal environment are not an OS network sandbox.
The generator has no network/database/provider code; hosts provide stronger
isolation when required and verify installed wheel/manifest identity before use.
Version 1 generates templates from FlowDefinition only. SourceSnapshot import is
explicitly unsupported by this capability; both paths still converge on Blueprint.

The development candidate requires SDK `0.1.0a3` and a Flow-capable CLI
`>=0.2.10,<0.3`. Candidate metadata is not publication. Publish the reviewed SDK
before this package, then update runtime dependency pins through their own release.

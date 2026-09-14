# Business Flow definitions

This package owns the nine-category, 28-recipe catalog and its configuration
fields. It includes one isolated Blueprint generator: HubSpot deals to Sanka
orders and draft invoices. The other 27 recipes have definitions only in this
package; the catalog reports this separately from generation support.

The generator describes an interval schedule, complete deal import into orders,
and draft invoices from the completed import's order output. It preserves existing
invoices, produces one invoice per order, and requires inactive construction.
It does not access HubSpot or run an import, invoice, schedule, or payment.

Hosts resolve the selected data endpoint and saved mapping inside the authenticated
workspace. Pass the mapping's immutable ID, revision and digest to `definition()`;
the package cannot authenticate an endpoint or establish mapping ownership.
The static catalog and `resolve_parameters()` supply the same settings for manual
configuration and AI proposals. Native action implementations and credentials stay
in the hosted API/jobs runtime.

`sanka-extension-business-flows` accepts one SDK `BlueprintRequest` on stdin and
returns one `BlueprintResponse` on stdout. The manifest exposes only `blueprint`.
Unknown selectors, changed template identity, incomplete configuration and missing
host capabilities are rejected. Stable logical workflow/node IDs preserve ownership
across repeated generation; the shared runtime owns planning and reconciliation.

This is a construction-definition candidate. Native v3 verification and activation
remain unavailable in the shared runtime. Successful generation or installation
does not establish native execution support. The released CLI 0.2.12 is too old;
the candidate manifest requires the SDK-a5 runtime release 0.2.13 or later.

Run `make build-business-flows` from the repository root to build the candidate
wheel and verify its dependency closure and manifest. Its separate marketplace
snapshot is `business-flows/`; the current Data/Code marketplace and published
wheels remain unchanged. Publication requires reviewed source and separate
authorization; the intended package tag is `business-flows-v0.1.0a1`.

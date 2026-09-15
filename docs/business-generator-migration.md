# Shared business generator migration

The next Business Flow candidate contains generators for the 27 native recipes
beyond HubSpot billing. They use four shared profile families: conversions,
ordered source approvals, assigned tasks and business processes. Each recipe has
one explicit binding in `native_recipes.py` and a separate reviewed input/output
fixture under `packages/sanka-extension-business-flows/tests/fixtures/native_recipes/`.

This source is not advertised by the published a1 executable, manifest or catalog
availability. The candidate requires SDK a7. Publish approved SDK a6 and then a7
before changing the Business Flow dependency pin, package version or manifest.
After publication, the a2 release must dispatch these selectors alongside the
existing HubSpot selector and pass wheel/discovery acceptance against the shared
runtime. The hosted adapter also needs translation and native parity evidence for
each recipe. Successful declarative generation does not prove native execution.

## Generation and review

`native_definition(recipe_id, parameters)` resolves initial manual or AI input
through the common catalog and the existing native normalization rules. Its
complete effective settings are visible in the plan. `native_capability` binds
the selector to the exact recipe metadata and family definition.
`generate_native` accepts a correlated SDK request and returns one inactive
Blueprint v5 workflow. It rejects a different recipe's profile, changed logical
identities, altered provenance, noncanonical effective settings and missing host
capabilities. It has no provider, database, dispatch or activation side effects.

The original catalog-resolved input remains separate from effective native
settings. Existing hosted generation receipt hashes use the former; normalizing
that stored request in place would break retries. Shared managed updates must
retain the original template baseline and compare it with native user edits.
They must not normalize an observed user edit as if it were fresh input.

Settings never determine workflow or node logical identities. The host stores
native UUID bindings once and preserves them when applying an approved update.
These generator tests prove stable logical IDs; the host must separately prove
native UUID preservation, concurrent-edit protection and save/reload behavior.

## Native compatibility details

- Task and employee selections accept decimal aliases and deduplicate canonical
  workspace-user IDs in input order. Approvals preserve reviewer order and reject
  duplicate numeric identities. Ticket owner pools require canonical decimal IDs
  after trimming and sort numerically. All membership checks remain hosted.
- Checklist titles reject collisions after trimming. Task name/status filters
  deduplicate after trimming. Native list limits are checked first.
- `open_stages` selects stage UUIDs; `won_stage` retains its exact stage text.
  The latter is resolved against the triggering Deal's pipeline by the host.
- Notes retain null, empty and whitespace values as distinct requested settings.
  Prices use bounded exact decimal conversion, without floating-point rounding.
- The current daily schedule resolves the workspace offset at construction.
  Recipes with a fixed 09:00 schedule reject an invented `local_hour` option.
  Automatic daylight-saving rescheduling is not part of these profiles.
- Reminder recipes create tasks. Sales delivery billing creates a delivery
  preparation task and draft invoice without waiting for delivery completion.
  Expense accounting creates review tasks without posting journals.

## Acceptance and remaining work

Each fixture names the recipe's full effective settings, stable node IDs and key
fixed policies. Tests also round-trip every request through an isolated installed
Python process outside the repository, reject cross-recipe substitution, preserve
generation request inputs, and verify settings changes keep logical IDs stable.
The existing a1 tests continue to exercise its unchanged executable behavior.

Before rollout, complete published SDK/package provenance, shared runtime v5
admission, private native translation parity, browser persistence and independent
execution verification for each recipe. Blueprint v5 remains construction-only;
HubSpot's eleven billing cases cannot establish approval, task or monthly billing
verification or authorize their activation.

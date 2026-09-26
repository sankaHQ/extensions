# Sanka Extensions

This repository owns the Apache-2.0 Sanka Extension SDK and independently installable
extensions used by the shared Sanka CLI and migration runtime. Extensions
implement data access through `sanka_extensions.app`; code extensions implement the
`sanka-extension/v1` lifecycle. Both interfaces are executable today.
Read `docs/naming-compatibility.md` before changing published identifiers.

## Boundaries

- `packages/sanka-extension-sdk` owns the canonical `sanka_extensions.app` and `sanka_extensions.code` interfaces. `packages/sanka-connector-sdk` preserves the dependency-free published data-access types; it is the unified SDK's only dependency.
- `sanka_extensions.flow` owns declarative business requests. `flow.create` has no
  execution side effects. Read `docs/flow.md` before changing its fixed reapplication
  and activation requirements; runtime enforcement and runnable Flow packages are
  separate from the SDK contract.
- Keep existing class identity across canonical and compatibility imports.
- The SDK must not depend on Sanka's AGPL runtime, database drivers, framework
  runtimes, or provider clients.
- Future data access extensions depend on the SDK and only the third-party
  libraries they need. None are currently published in this marketplace.
- Published extension entry points use the `sanka.connectors` group and resolve to a
  `sanka_extensions.app.ExtensionRegistration`.
- Extension code must never import `sanka`, `sanka.runtime`, or another extension.
- Do not add arbitrary in-process hooks. New extension kinds need typed, versioned
  contracts, isolated execution, deterministic discovery, and fail-closed
  capability validation.
- SaaS and managed-service providers such as HubSpot, Salesforce, and SendGrid
  are hosted Sanka API capabilities. Do not add their credentials, clients,
  adapters, or entry points to this repository.
- Keep all source files Apache-2.0 and retain SPDX headers.

## Development

```bash
uv sync --all-packages
uv run ruff check .
uv run ruff format --check .
uv run mypy packages scripts
uv run pytest
```

Run extension integration tests only when their documented environment variable
is configured. They must skip cleanly otherwise.

## Tests

Use the `test-audit` skill from the sanka-project workspace when it is
available; in a standalone checkout the rules below are complete on their own.

- Before adding a test, state in the PR which failure it catches (for new
  behaviour: the specified behaviour not holding) and why no existing test at a
  stronger boundary already catches it. Tests for new behaviour go at the single
  boundary that owns it; a test that cannot name a failure is not added.
- One owner per behaviour. Extension behaviour is owned by a test through its
  published interface (registration, readers and writers, the
  `sanka-extension/v1` lifecycle) against real inputs. Unit tests are for pure
  logic with real branching: parsers, mappers, converters. Share one contract
  suite across converters instead of copying it per package.
- Never write a test that reads source, workflow, Makefile, manifest, doc or
  config files as text and asserts on the text; asserts a constant, label,
  error wording or URL literal verbatim; only asserts that a mock was called;
  asserts `hasattr`/`callable`/`isinstance`; asserts that an entry point or
  extension exists or is registered; or computes the expected value with the
  code under test.
- Documented compatibility contracts are the exception to the literal rule:
  published identifiers, compatibility imports, error codes and retained URLs
  are asserted exactly, once, at their owning boundary.
- Do not write unit tests after the code to cover a diff. A regression test must
  fail on the pre-fix code; say so in the PR.
- Test lines added in a PR may not exceed non-test lines added unless the PR
  explains why (bug reproduction, new pure module, table-driven cases).
- Extend a table or `parametrize` row instead of copying a test. Split or trim a
  test file above 1,500 lines before adding anything to it.
- Fix a unit test slower than 0.5 s. Never add sleeps, real timers or real
  network waits.
- When a behaviour-preserving refactor breaks tests, delete or rewrite them at
  the owning boundary. Do not edit assertions to match the new implementation.
- No meta-tests that require other tests, docs listings or registrations to exist.
- Deleting a low-value test is a valid change on its own. Report test and
  non-test line counts separately in the PR.

## Releases

Publish an SDK before packages that implement its interface. All AI-authored
changes use the workspace `sanka-pr-flow`; never publish from an unreviewed
branch.

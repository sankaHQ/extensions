# Sanka Extensions

This repository owns the Apache-2.0 Sanka Extension SDK and independently installable
extensions used by the shared Sanka CLI and migration runtime. Extensions
implement data access through `sanka_extensions.data`; code extensions implement the
`sanka-extension/v1` lifecycle. Both interfaces are executable today.
Read `docs/naming-compatibility.md` before changing published identifiers.

## Boundaries

- `packages/sanka-extension-sdk` owns the canonical `sanka_extensions.data`, `.flow` and `.code` interfaces. `packages/sanka-connector-sdk` preserves the dependency-free published data-access types; it is the unified SDK's only dependency.
- `sanka_extensions.flow` owns declarative business requests. `flow.create` has no
  execution side effects. Read `docs/flow.md` before changing its fixed reapplication
  and activation requirements. Flow packages generate Blueprints through the
  isolated `sanka-flow-extension/v1` protocol; native execution belongs to the
  host. Keep static Flow discovery in `flow-marketplace.json` so old clients can
  continue reading the Data/Code catalog.
- Keep existing class identity across canonical and compatibility imports.
- The SDK must not depend on Sanka's AGPL runtime, database drivers, framework
  runtimes, or provider clients.
- Each `packages/sanka-connector-*` extension depends on the SDK and only
  the third-party libraries that extension needs.
- Published extension entry points use the `sanka.connectors` group and resolve to a
  `sanka_extensions.data.ExtensionRegistration`.
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

## Releases

Publish an SDK before packages that implement its interface. All AI-authored
changes use the workspace `sanka-pr-flow`; never publish from an unreviewed
branch.

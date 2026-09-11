# Sanka Extensions

This repository owns Apache-2.0 extension SDKs and independently installable
extensions used by the shared Sanka CLI and migration runtime. Data extensions
implement system access through `sanka_data`; code extensions implement the
`sanka-extension/v1` lifecycle. Both interfaces are executable today.
Read `docs/naming-compatibility.md` before changing published identifiers.

## Boundaries

- `packages/sanka-connector-sdk` contains the canonical `sanka_data` interfaces and compatibility re-exports: protocols, typed records, capability
  declarations, credentials, errors, and entry-point registration only.
- The SDK must not depend on Sanka's AGPL runtime, database drivers, framework
  runtimes, or provider clients.
- Each `packages/sanka-connector-*` data extension depends on the SDK and only
  the third-party libraries that extension needs.
- Published data-extension entry points use the `sanka.connectors` group and resolve to a
  `sanka_data.DataExtensionRegistration`.
- Extension code must never import `sanka`, `sanka.runtime`, or another extension.
- Do not add arbitrary in-process hooks. New extension kinds need typed, versioned
  contracts, isolated execution, deterministic discovery, and fail-closed
  capability validation.
- SaaS and managed-system providers such as HubSpot, Salesforce, and SendGrid
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

Run data-extension integration tests only when their documented environment variable
is configured. They must skip cleanly otherwise.

## Releases

Publish an SDK before packages that implement its interface. All AI-authored
changes use the workspace `sanka-pr-flow`; never publish from an unreviewed
branch.

# Sanka Extensions

This repository owns Apache-2.0 extension SDKs and independently installable
extensions used by the Sanka migration runtime. The current executable extension interface
is the Connector SDK; new framework, language, library, or generation extension kinds
must establish a typed interface and boundary checks before adding packages.

## Boundaries

- `packages/sanka-connector-sdk` contains protocols, typed records, capability
  declarations, credentials, errors, and entry-point registration only.
- The SDK must not depend on Sanka's AGPL runtime, database drivers, framework
  runtimes, or provider clients.
- Each `packages/sanka-connector-*` connector extension depends on the SDK and only
  the third-party libraries that extension needs.
- Connector entry points use the `sanka.connectors` group and resolve to a
  `sanka_connector.ConnectorRegistration`.
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

Run connector integration tests only when their documented environment variable
is configured. They must skip cleanly otherwise.

## Releases

Publish an SDK before packages that implement its interface. All AI-authored
changes use the workspace `sanka-pr-flow`; never publish from an unreviewed
branch.

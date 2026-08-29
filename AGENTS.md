# Sanka Connectors

This repository owns the Apache-2.0 Connector SDK and first-party connector
distributions used by the Sanka migration runtime.

## Boundaries

- `packages/sanka-connector-sdk` contains protocols, typed records, capability
  declarations, credentials, errors, and entry-point registration only.
- The SDK must not depend on Sanka's AGPL runtime, database drivers, framework
  runtimes, or provider clients.
- Each `packages/sanka-connector-*` provider depends on the SDK and only the
  third-party libraries that provider needs.
- Connector entry points use the `sanka.connectors` group and resolve to a
  `sanka_connector.ConnectorRegistration`.
- Provider code must never import `sanka`, `sanka.runtime`, or another provider.
- Keep all source files Apache-2.0 and retain SPDX headers.

## Development

```bash
uv sync --all-packages
uv run ruff check .
uv run ruff format --check .
uv run mypy packages
uv run pytest
```

Run connector integration tests only when their documented environment variable
is configured. They must skip cleanly otherwise.

## Releases

Publish the SDK before provider packages. All AI-authored changes use the
workspace `sanka-pr-flow`; never publish from an unreviewed branch.

# Sanka Connectors

Apache-2.0 connector interfaces and local/offline provider plugins for the
[Sanka migration runtime](https://github.com/sankaHQ/sanka).

The repository is a Python 3.12+ `uv` workspace. The core SDK is deliberately
small and has no runtime dependencies:

- `sanka-connector-sdk` — typed source/destination protocols, records,
  capabilities, credentials, errors, and entry-point registration
- `sanka-connector-clickhouse` — ClickHouse destination
- `sanka-connector-csv` — CSV source
- `sanka-connector-markdown` — Markdown source
- `sanka-connector-postgres` — PostgreSQL source and destination
- `sanka-connector-sqlite` — SQLite source and destination

Provider packages advertise themselves through the `sanka.connectors` Python
entry-point group. Installing one adds that provider to Sanka without adding its
driver or client dependencies to the base Sanka installation.

HubSpot, Salesforce, SendGrid, and other SaaS/system migrations run through
Sanka's hosted System Migration API. Their credentials and provider runtimes
stay in Sanka's managed service; they are not local connector packages.

## Development

```bash
uv sync --all-packages
make check
```

See [Connector development](docs/connector-development.md) for the interface
boundary and provider rules. See [Releasing](docs/releasing.md) for the
SDK-first, trusted-publishing release gate.

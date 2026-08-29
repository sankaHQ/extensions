# Sanka Connectors

Apache-2.0 connector interfaces and first-party provider plugins for the
[Sanka migration runtime](https://github.com/sankaHQ/sanka).

The repository is a Python 3.12+ `uv` workspace. The core SDK is deliberately
small and has no runtime dependencies:

- `sanka-connector-sdk` — typed source/destination protocols, records,
  capabilities, credentials, errors, and entry-point registration
- `sanka-connector-clickhouse` — ClickHouse destination
- `sanka-connector-csv` — CSV source
- `sanka-connector-hubspot` — HubSpot source and destination
- `sanka-connector-markdown` — Markdown source
- `sanka-connector-postgres` — PostgreSQL source and destination
- `sanka-connector-salesforce` — Salesforce source
- `sanka-connector-sendgrid` — SendGrid source
- `sanka-connector-sqlite` — SQLite source and destination

Provider packages advertise themselves through the `sanka.connectors` Python
entry-point group. Installing one adds that provider to Sanka without adding its
driver or client dependencies to the base Sanka installation.

## Development

```bash
uv sync --all-packages
make check
```

See [Connector development](docs/connector-development.md) for the interface
boundary and provider rules. See [Releasing](docs/releasing.md) for the
SDK-first, trusted-publishing release gate.

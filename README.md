# Sanka Mods

Apache-2.0 extension interfaces and independently installable mods for the
[Sanka migration runtime](https://github.com/sankaHQ/sanka).

Sanka owns the migration lifecycle—`scan`, `plan`, `apply`, `test`, and
`verify`. Mods own stack-specific detection, inspection, generation, and data
access. Keeping those concerns here lets the base runtime stay small while
developers contribute support for frameworks, databases, languages, libraries,
and file formats without adding every optional dependency to Sanka itself.

The repository is a Python 3.12+ `uv` workspace. Its first stable mod interface
is the deliberately small, zero-dependency Connector SDK:

- `sanka-connector-sdk` — typed source/destination protocols, records,
  capabilities, credentials, errors, and entry-point registration
- `sanka-connector-clickhouse` — ClickHouse destination
- `sanka-connector-csv` — CSV source
- `sanka-connector-markdown` — Markdown source
- `sanka-connector-postgres` — PostgreSQL source and destination
- `sanka-connector-sqlite` — SQLite source and destination

Connector mods advertise themselves through the existing `sanka.connectors`
Python entry-point group. Installing one adds that capability to Sanka without
adding its driver dependencies to the base Sanka installation. These published
package names and entry points remain stable after the repository rename.

Framework and code-transformation mods—for example Flask inspection or a future
framework target—will use typed mod interfaces added here. They must not import
Sanka's AGPL runtime or use ad hoc in-process hooks. See [The mod
model](docs/mods.md) for the contribution boundary and planned resolver flow.

Today Sanka discovers connector mods that are already installed. It does not
silently download arbitrary packages during `scan` or `plan`. The resolver work
will let Sanka fingerprint a project, select only reviewed mods required for
that project, materialize exact versions in an isolated environment, and record
their versions and hashes in the plan.

HubSpot, Salesforce, SendGrid, and other SaaS/system migrations run through
Sanka's hosted System Migration API. Their credentials and provider runtimes
stay in Sanka's managed service; they are not local connector packages.

## Development

```bash
uv sync --all-packages
make check
```

See [The mod model](docs/mods.md) for extension and resolution rules,
[Connector-mod development](docs/connector-development.md) for the current
interface boundary, and [Releasing](docs/releasing.md) for the SDK-first,
trusted-publishing release gate.

# Sanka Extensions

Extensions for the open source [Sanka CLI](https://github.com/sankaHQ/sanka).
Install one and `sanka scan`, `plan`, `apply`, `test` and `verify` gain a
migration path (for example Django REST Framework to FastAPI) or a data endpoint
type (for example PostgreSQL). Apache-2.0, Python 3.12+.

Extensions add capabilities to Sanka (data migrations), Sanka Flow (workflow
migrations) and Sanka Code (code migrations); data endpoints are the configured
sources and destinations those capabilities read from and write to.

[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![Catalog](https://img.shields.io/badge/catalog-docs%2Fcatalog.md-ff5a1f)](docs/catalog.md)
[![CLI](https://img.shields.io/pypi/v/sanka-cli?label=sanka-cli)](https://pypi.org/project/sanka-cli/)

## What you can install today

| Extension | What it does |
| --- | --- |
| `sanka/drf-to-fastapi` | Converts a Django REST Framework app to native async FastAPI ([guide](https://sanka.com/docs/developers/migrate/django-to-fastapi/)) |
| `sanka/drf-to-flask` | Converts a Django REST Framework app to Flask ([guide](https://sanka.com/docs/developers/migrate/django-to-flask/)) |
| `sanka/postgres` | Reads and writes PostgreSQL records for data migrations |
| `sanka/sqlite` | Reads and writes SQLite records |
| `sanka/csv` | Reads CSV files as a migration source |
| `sanka/markdown` | Reads Markdown documents as a migration source |
| `sanka/clickhouse` | Writes to ClickHouse as a migration destination |

The generated [catalog](docs/catalog.md) is the authoritative list, with roles
and versions checked against `marketplace.json` and each manifest. Experimental
converters are published as scoped prereleases outside the default catalog:
`sanka/python-to-golang` and `sanka/typescript-to-rust`
([`api-converters-v0.1.0a1`](docs/api-converter-release.md)) and
`sanka/react-native-to-native`
([`mobile-converters-v0.1.0a1`](docs/mobile-converter-release.md)); each package
README shows how to pin them.

## Install

```bash
uv tool install --python 3.12 sanka-cli      # the CLI, if you do not have it yet
sanka extension add sanka/drf-to-fastapi     # this marketplace is preconfigured and trusted
sanka extension list
```

Each extension is an immutable GitHub release wheel. Before anything runs, the
CLI checks the manifest, the release URL, the SHA-256 digest and the CLI
compatibility range, then installs the wheel into an isolated environment with
`pip --isolated --no-index --no-deps --require-hashes`. PyPI is never a
fallback, and a missing or mismatched hash stops execution instead of
substituting another version.

Installing a data extension makes a capability available; it does not connect
to anything. Sources and destinations are configured separately as data
endpoints with their own credentials. Hosted SaaS systems such as HubSpot,
Salesforce and SendGrid are capabilities of the hosted Sanka API, not local
extensions.

Other marketplaces need explicit `--trust`; adding one pins an immutable
snapshot, and `--revision FULL_COMMIT_SHA` pins a specific catalog:

```bash
sanka extension marketplace add PATH_OR_GIT_URL --name third-party --trust
```

## Build your own

The Sanka Extension SDK (`sanka_extensions`) is published as release wheels.
Install the latest `sdk-v*` release into a Python 3.12 virtual environment, not
into the CLI's tool environment:

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install \
  https://github.com/sankaHQ/extensions/releases/download/sdk-v0.1.0a7/sanka_connector_sdk-0.1.0a12-py3-none-any.whl \
  https://github.com/sankaHQ/extensions/releases/download/sdk-v0.1.0a7/sanka_extension_sdk-0.1.0a7-py3-none-any.whl
```

| Interface | Use it for |
| --- | --- |
| `sanka_extensions.code` | Code migrations: typed requests and responses for `scan`, `plan`, `apply`, `test` and `verify` |
| `sanka_extensions.data` | Data migrations: readers, writers, records, capabilities and registration |
| `sanka_extensions.flow` | Workflow migrations: a declarative definition contract only; no runnable Flow packages are published yet |

Start with the [SDK guide](docs/sdk.md) and
[extension development](docs/extension-development.md), which covers the
`sanka-extension/v1` protocol. [Flow contracts](docs/flow.md) and the
[compatibility guide](docs/naming-compatibility.md) explain the published
identifiers that stay stable across releases.

## Run an experimental extension from this checkout

An extension that is not catalogued yet, or one you are developing, runs through
the same protocol the CLI uses:

```bash
uv sync --frozen --all-packages
uv run python scripts/fetch_typescript_bundle.py   # TypeScript-based extensions only
uv run python scripts/run_extension.py sanka/typescript-to-rust scan  --project ~/app --config database_layer=sqlx
uv run python scripts/run_extension.py sanka/typescript-to-rust plan  --project ~/app --config database_layer=sqlx
uv run python scripts/run_extension.py sanka/typescript-to-rust apply --project ~/app --config database_layer=sqlx --plan-hash sha256:...
```

`apply` requires the exact hash that `plan` printed; artifacts live under
`<project>/.sanka/<extension name>/`; `test` and `verify` need the toolchains and
fixture databases documented in each package README.

## Development

```bash
uv sync --frozen --all-packages
make check          # format, types, boundaries, terminology, catalog, tests
make build-release  # builds and validates every wheel; does not publish
```

After changing a package, run `make update-marketplace-hashes`, review the
manifest diff and validate again. Releases are documented in
[releasing.md](docs/releasing.md); ownership and licensing boundaries in
[AGENTS.md](AGENTS.md). Converter changes also pass the converter benchmark gate
described in [converter-regression.md](docs/converter-regression.md).

## Contributing

Open or reuse an issue first, agree on scope for substantial changes, and keep
every source file Apache-2.0 with its SPDX header. See
[CONTRIBUTING.md](CONTRIBUTING.md) and [SUPPORT.md](SUPPORT.md).

# Sanka Extensions

Extensions for the open source [Sanka CLI](https://github.com/sankaHQ/sanka).
Install one and `sanka scan`, `plan`, `apply`, `test` and `verify` gain a
migration path (for example Django REST Framework to FastAPI). Apache-2.0,
Python 3.12+.

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
| `sanka/python-to-golang` | Converts supported DRF, FastAPI and Flask APIs to Go, including SQLite and PostgreSQL ([guide](https://sanka.com/docs/developers/migrate/python-to-go/)) |

The generated [catalog](docs/catalog.md) is the authoritative list, with roles
and versions checked against `marketplace.json` and each manifest. Experimental
converters also appear in the official catalog. Go and Rust use the scoped
[API converter release](docs/api-converter-release.md), and
`sanka/react-native-to-native`
([`mobile-converters-v0.1.0a1`](docs/mobile-converter-release.md)); each package
README describes its supported scenarios and historical release pins. Package
READMEs also form immutable wheel metadata; use this guide for current installation.

## Install

```bash
uv tool install --upgrade --python 3.12 'sanka-cli==0.3.9'
sanka extension marketplace list
sanka extension marketplace upgrade official
sanka extension add sanka/drf-to-fastapi     # this marketplace is preconfigured and trusted
sanka extension list
```

Commands use the CLI by default. Add `--tui` or run `sanka tui` for the optional
interactive interface. Both use the same Plan configuration and endpoint scope.
Updated CLI planning choices use language-qualified targets such as
`python-fastapi`, `python-flask` and `go-chi` (also `go-fiber`, `go-gin`, `go-mux`).
Short names remain compatible; extension package IDs and published manifest
identities stay unchanged. See [naming compatibility](docs/naming-compatibility.md).
Python-to-Go Scan discovers the source framework, entrypoint, models and database.
Plan selects the Go router, destination database and endpoints; the destination
defaults to the detected source database. Ambiguous source inputs require a choice.
The Go SQLite example needs no container; PostgreSQL examples need a disposable
database. See the [Go package README](packages/sanka-extension-python-to-golang/README.md).
Apply writes a reviewed `README.md` into the generated Go project, with database
migration, API startup and native test commands for its selected router.
Go Test and Verify report fixture setup, native Go tests and each captured HTTP
scenario's outcome. Verify compares source and Go responses, rows and sequences
where captured; it does not certify behaviors outside those scenarios. The
saved reports include test counts and per-scenario results for CLI summaries.

For example, the gadget-inventory fixture can report:

```text
MATCH PUT /api/gadgets/1/ [replace] — source 200 / Go 200; status, JSON body, media type, rows and sequences
```

The CLI summary lists every planned method. An endpoint without a replay scenario
is labelled `not exercised`; add a scenario with suitable setup and expected status
to cover it. Passing scenario comparisons do not certify every possible input.

Each extension is an immutable GitHub release wheel. Before anything runs, the
CLI checks the manifest, the release URL, the SHA-256 digest and the CLI
compatibility range, then installs the wheel into an isolated environment with
`pip --isolated --no-index --no-deps --require-hashes`. PyPI is never a
fallback, and a missing or mismatched hash stops execution instead of
substituting another version.

The official marketplace currently publishes code migration extensions. Hosted
SaaS systems such as HubSpot, Salesforce and SendGrid are capabilities of the
hosted Sanka API, not local extensions.

Other marketplaces need explicit `--trust`; adding one pins an immutable
snapshot, and `--revision FULL_COMMIT_SHA` pins a specific catalog:

```bash
sanka extension marketplace add PATH_OR_GIT_URL --name third-party --trust
```

## Build your own

The published SDK `0.1.0a8` provides `sanka_extensions.app`. Install it in a
separate Python 3.12 environment for extension development:

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install \
  'https://github.com/sankaHQ/extensions/releases/download/sdk-v0.1.0a8/sanka_connector_sdk-0.1.0a12-py3-none-any.whl#sha256=34da5c35aaa60fc19258e76b72a3eca58bf52fff96e2ccf9a0aa1115f8878d8e' \
  'https://github.com/sankaHQ/extensions/releases/download/sdk-v0.1.0a8/sanka_extension_sdk-0.1.0a8-py3-none-any.whl#sha256=faa18386a9376e7477f6465f56de106111071c84c4f6e5d65dd273b988cf3377'
```

| Interface | Use it for |
| --- | --- |
| `sanka_extensions.code` | Code migrations: typed requests and responses for `scan`, `plan`, `apply`, `test` and `verify` |
| `sanka_extensions.app` | Application data access: readers, writers, records, capabilities and registration |
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

### CI test ownership

The general check runs the SDK, shared Python helpers, DRF converters, Go and Jev
unit/contract suites. Rust, React Native and the TypeScript parser run their
complete suites once in the dedicated installed-wheel jobs, including native
compilation, source parity and tampering failures. Two subprocess checks also
exercise Rust/mobile against the candidate SDK in the general job; the full
installed-wheel suites use the published SDK.

Flask backend qualification runs against its installed wheel. Django 6 retains
all source capture, HTTP/database parity and source rejection cases; 34 pure
runtime/parser cases run only in the general check. Both SQLite and PostgreSQL
fixtures remain covered. API/mobile release validators run in their release
workflows instead of repeating in general CI.

DRF-to-FastAPI and DRF-to-Flask replay tests run their complete passing baselines,
then only affected requests for mutation controls. FastAPI auth/nested generation
assertions share the parity test's candidate. Business Flow checks all 27 recipe
round trips in one isolated process, retaining each recipe's independent expected
configuration, capability rejection and identity tests. SDK and Jev trust-boundary
cases remain intact; reducing test count alone is not a reason to remove them.

Public CLI acceptance remains separate from native extension tests because it
tests installation and CLI transport. Local `make check` still discovers every
suite; CI-only exclusions do not change local defaults. Superseded PR runs cancel,
while main and release runs remain independent.


## Contributing

Open or reuse an issue first, agree on scope for substantial changes, and keep
every source file Apache-2.0 with its SPDX header. See
[CONTRIBUTING.md](CONTRIBUTING.md) and [SUPPORT.md](SUPPORT.md).

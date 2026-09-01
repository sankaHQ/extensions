# Sanka Extensions

Apache-2.0 connector and migration-extension packages for the
[Sanka migration runtime](https://github.com/sankaHQ/sanka). This is a Python
3.12+ `uv` workspace.

Sanka owns the `scan -> plan -> apply -> test -> verify` lifecycle. Packages in
this repository provide connector drivers or stack-specific migration code
without adding those dependencies to the core runtime.

## Packages

| Package | Role |
| --- | --- |
| `sanka-connector-sdk` | Dependency-free connector protocols, records, capabilities, credentials, errors, and registration |
| `sanka-connector-clickhouse` | ClickHouse destination connector |
| `sanka-connector-csv` | CSV source connector |
| `sanka-connector-markdown` | Markdown source connector |
| `sanka-connector-postgres` | PostgreSQL source and destination connector |
| `sanka-connector-sqlite` | SQLite source and destination connector |
| `sanka-extension-sdk` | Dependency-free `sanka-extension/v1` subprocess contract |
| `sanka-extension-drf-to-fastapi` | Executable Django REST Framework to FastAPI migration extension |

Connector packages register one `sanka.connectors` Python entry point. The
executable migration extension does not load into the Sanka process; it exchanges
validated JSON with the runtime over standard input and output.

HubSpot, Salesforce, SendGrid, and other hosted-system providers remain Sanka API
capabilities. They are not local packages in this workspace.

## Official extension

The official catalog currently contains one executable extension:
`sanka/drf-to-fastapi`.

- Distribution and executable: `sanka-extension-drf-to-fastapi`
- Match: Python projects with `djangorestframework` plus `manage.py` or a static
  `rest_framework` import
- Target: `fastapi`
- Commands: `scan`, `plan`, `apply`, `test`, and `verify`
- Runtime contract: `sanka-extension/v1`

The package depends only on the matching `sanka-extension-sdk` version. It
inspects DRF projects, produces a reviewed migration plan, generates FastAPI
output, and tests and verifies that output through the same subprocess contract.

## Catalog and manifest

The catalog is deliberately small:

```text
marketplace.json
packages/
  sanka-extension-drf-to-fastapi/
    extension.json
```

[`marketplace.json`](marketplace.json) uses `sanka-marketplace/v1` and maps an
extension ID to its manifest. The
[`extension.json`](packages/sanka-extension-drf-to-fastapi/extension.json)
manifest uses `sanka-extension-manifest/v1` and fixes:

- the extension ID, version, target, commands, and project-match rules;
- the compatible `sanka-migrate` version range;
- the distribution name and console-script executable;
- every wheel filename, release URL, and SHA-256 digest.

A catalog update is incomplete until the built wheel bytes match those hashes.

## JSON subprocess contract

An extension reads one JSON request from standard input and writes one JSON
response to standard output. Diagnostics go to standard error. Both documents
use `"schema_version": "sanka-extension/v1"`.

A request contains these exact fields:

```text
schema_version, request_id, command, project_root, artifact_root,
extension { id, version, manifest_digest }, fingerprint, configuration,
prior_artifacts, reviewed_plan_hash
```

`project_root` and `artifact_root` must be absolute. `command` must be one of the
five lifecycle commands, and `manifest_digest` must be a lowercase SHA-256
digest.

A response contains these exact fields:

```text
schema_version, request_id, command, extension { id, version }, outcome,
data, artifacts, limitations, next_actions
```

`outcome` is `success` or `error`. Error responses also require
`error { code, message, details }`. The runtime checks the request ID, command,
extension identity, artifact paths, and the complete response shape. Missing or
extra fields fail the request.

## Trust, pins, and wheel execution

The runtime trusts the official `github.com/sankaHQ/extensions` identity. Every
other local or Git marketplace requires an explicit `--trust` flag:

After a `sanka-migrate` release pins the published default extension package:

```bash
sanka-migrate extension marketplace add git@github.com:sankaHQ/extensions.git --name sanka --json
sanka-migrate extension marketplace add PATH_OR_GIT_URL --name third-party --trust --json
sanka-migrate extension add sanka/drf-to-fastapi --marketplace sanka --json
sanka-migrate extension list --json
```

Adding a marketplace records an immutable snapshot. Git sources pin the resolved
commit; local sources pin a content digest. Adding an extension writes
`.sanka/extensions.lock` with the marketplace identity, snapshot digest,
manifest digest, package version, artifact digest, protocol, executable,
commands, and configuration digest. A marketplace upgrade creates a new
snapshot but does not rewrite an existing project pin.

Extension artifacts are wheels, not source distributions. For isolated
materialization, Sanka installs only the manifest-listed wheel bytes with
`pip --isolated --no-index --no-deps --require-hashes`. It verifies wheel
metadata, dependencies, imports, entry points, cached hashes, and the installed
environment before execution. The official extension's installed wheel content
is checked against its lock and manifest before its executable is leased.

Missing trust, a missing exact wheel or cache entry, version incompatibility,
digest drift, an invalid executable path, or a malformed response stops the
lifecycle with a stable error code. The runtime does not choose a different
version or implementation as a fallback.

## Local development

```bash
uv sync --frozen --all-packages
make check
```

`make check` runs Ruff, format checking, mypy, dependency-boundary validation,
the package tests, and the catalog hash updater tests. Run the executable
extension slice directly with:

```bash
uv run pytest packages/sanka-extension-sdk/tests \
  packages/sanka-extension-drf-to-fastapi/tests
```

Package code must retain its Apache-2.0 SPDX header. SDKs must not import the
AGPL runtime. A connector may import its SDK and its own driver; an executable
extension may import `sanka-extension-sdk`, but not `sanka`, `sanka.runtime`, or
another extension.

## Release validation

```bash
make build-release
uv run python scripts/check_release_tag.py extensions extensions-v0.1.0a1 tag
```

`make build-release` builds a wheel and source distribution for all eight
packages, writes the combined set under `release/all`, records `SOURCE_COMMIT`
and `SHA256SUMS`, and runs `scripts/check_release_artifacts.py`. That validator
checks package counts, versions, dependencies, entry points, and catalog hashes.
It does not publish.

When extension wheel bytes intentionally change, regenerate and verify the two
manifest hashes with `make update-marketplace-hashes`, then review the
`extension.json` diff before committing it. Publish the extension SDK before the
DRF-to-FastAPI package.

Connector development details are in
[`docs/connector-development.md`](docs/connector-development.md).

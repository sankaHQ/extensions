# Sanka Extensions

Apache-2.0 extensions and SDKs for the [shared Sanka CLI and migration runtime](https://github.com/sankaHQ/sanka). This is a Python 3.12+ `uv` workspace.

**Extensions add capabilities. Systems identify the databases, files, and SaaS accounts used in a migration.** One PostgreSQL extension can serve several independently configured PostgreSQL systems.

| Product | Work |
| --- | --- |
| Sanka | System and data migrations |
| Sanka Flow | Workflow migrations and operation of the resulting workflows |
| Sanka Code | Application and code migrations |

The PostgreSQL data extension reads and writes database records. Application SQL or ORM changes belong to Sanka Code; a project can require both products. These responsibilities do not imply support for every database-service migration route. DRF-to-FastAPI and DRF-to-Flask are code extensions that convert applications.

## Available extensions

The current catalog is documented in [catalog.md](docs/catalog.md), generated and checked against `marketplace.json` and each manifest. It contains data and code extensions.

| SDK / supporting package | Responsibility |
| --- | --- |
| `sanka-connector-sdk` | Data Extension SDK: `sanka_data` system readers, writers, records, capabilities, credentials, errors, and registration |
| `sanka-extension-sdk` | Code Extension SDK: typed `sanka-extension/v1` subprocess messages |
| `sanka-drf-replay` | Framework-independent request/response replay support for code extensions |

Package distribution names are exact installation identities. The data SDK distribution and data-extension distributions retain their published names during the [compatibility transition](docs/naming-compatibility.md). New developer examples use `sanka_data`, `DataExtensionRegistration`, `SystemReader`, and `SystemWriter`.

HubSpot, Salesforce, SendGrid, and other hosted SaaS implementations remain private Sanka API capabilities. They are systems; users do not install them as local extensions.

## Install and inspect

```bash
sanka extension marketplace add git@github.com:sankaHQ/extensions.git --name sanka --json
sanka extension marketplace add PATH_OR_GIT_URL --name third-party --trust --json
sanka extension add sanka/postgres --marketplace sanka --json
sanka extension list --json
```

Installation makes a capability available. It does not authenticate any system or verify its reachability. Configure each system with its own endpoint and credential references before planning a data migration. Code extensions operate on projects and do not require a connected-system status.

## Catalog and manifests

`marketplace.json` uses `sanka-marketplace/v1`. Each manifest uses `sanka-extension-manifest/v2` and pins:

- extension ID and version;
- compatible `sanka-cli` versions;
- exact distribution identity and executable or data entry point;
- code lifecycle commands and project matching, or supported system types and read/write roles;
- wheel filenames, immutable release URLs, and SHA-256 digests.

The published manifest values `kind="connector"` (Data) and `kind="migration"` (Code), the `providers` field, and `sanka.connectors` entry points remain wire compatibility contracts. Keep the two typed execution protocols distinct. See [the transition map](docs/naming-compatibility.md).

## Execution and trust

Sanka owns discovery, planning, execution, and verification. Data extensions expose typed system readers and writers through the isolated data-extension host. Code extensions exchange validated JSON over standard input/output using `sanka-extension/v1`; diagnostics go to standard error.

A code-extension request contains:

```text
schema_version, request_id, command, project_root, artifact_root,
extension { id, version, manifest_digest }, fingerprint, configuration,
prior_artifacts, reviewed_plan_hash
```

The response contains:

```text
schema_version, request_id, command, extension { id, version }, outcome,
data, artifacts, limitations, next_actions
```

Error responses include `error { code, message, details }`. The runtime checks request identity, command, extension identity, artifact paths, and the complete response shape. Missing or extra fields fail the request.

The official `github.com/sankaHQ/extensions` marketplace is trusted. Other marketplaces require explicit `--trust`. Adding a marketplace pins an immutable snapshot. Installing an extension records exact manifest, artifact, configuration, and protocol identities in `.sanka/extensions.lock`; refreshing a marketplace does not rewrite existing project pins.

Only manifest-listed wheels are installed, with `pip --isolated --no-index --no-deps --require-hashes`. The runtime verifies wheel metadata, dependency closure, entry points, cached hashes, and the installed environment before execution. A subprocess is an execution boundary, not a complete operating-system sandbox.

Missing trust, artifacts, compatibility, or matching hashes stops execution. The runtime does not silently substitute another version or implementation.

## Development and release validation

```bash
uv sync --frozen --all-packages
make check
make build-release
```

`make check` runs formatting, type checks, dependency boundaries, terminology/catalog checks, package tests, and catalog updater tests. Database integration tests require their documented test endpoints and skip when absent.

`make build-release` builds and validates artifacts; it does not publish. After intentional wheel changes, run `make update-marketplace-hashes`, review the manifest diff, and validate again. Publish the Data Extension SDK before its implementing extensions, and publish SDK changes before advancing runtime dependency pins. Release publication requires a reviewed release and separate authorization.

See [data-extension development](docs/data-extension-development.md) and [AGENTS.md](AGENTS.md) for ownership and licensing boundaries.

The converter benchmark gate pins a reviewed benchmark revision and covers DRF-to-FastAPI and DRF-to-Flask. Fully generated candidates must pass the generated-scope tests and independent benchmark gates. Partial candidates disclose route gaps. Private benchmark fixtures and reports remain outside this repository.

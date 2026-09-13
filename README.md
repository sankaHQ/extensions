# Sanka Extensions

Apache-2.0 extensions and the Sanka Extension SDK for the [shared Sanka CLI and migration runtime](https://github.com/sankaHQ/sanka). This is a Python 3.12+ `uv` workspace.

**Extensions add data, workflow or code capabilities. Data endpoints identify the source or destination databases, files and SaaS accounts.** One PostgreSQL extension can serve several independently configured data endpoints.

| Product | Work |
| --- | --- |
| Sanka | Data migrations: records, schemas, relationships and attachments |
| Sanka Flow | Workflow migrations and reconstruction: automations, triggers, actions and conditions |
| Sanka Code | Code migrations: applications, SQL dialects, ORM and dbt models |

The PostgreSQL extension reads and writes database records. Application SQL or ORM changes belong to Sanka Code; a project can require both products. These responsibilities do not imply support for every database-service migration route. DRF-to-FastAPI and DRF-to-Flask are code extensions that convert applications.

## Available extensions

The [catalog](docs/catalog.md) groups extensions into **Data**, **Workflow**, and **Code**, generated and checked against `marketplace.json` and each manifest. Data and Code packages are available; Workflow currently has an SDK definition contract and no executable marketplace packages.

## Sanka Extension SDK

Use `sanka_extensions` to build extensions. The published SDK is distributed as
GitHub release wheels, including its exact compatibility dependency:

```bash
python -m pip install \
  https://github.com/sankaHQ/extensions/releases/download/extensions-v0.1.0a18/sanka_connector_sdk-0.1.0a12-py3-none-any.whl \
  https://github.com/sankaHQ/extensions/releases/download/extensions-v0.1.0a18/sanka_extension_sdk-0.1.0a2-py3-none-any.whl
```

This installs the released `0.1.0a2` SDK. The `0.1.0a3` SDK and v2 Flow contract
on this branch are release candidates; develop them with `uv sync --frozen
--all-packages`. They are not yet available through a published marketplace.

| Interface | Purpose |
| --- | --- |
| `sanka_extensions.data` | Data readers, writers, records, capabilities, credentials, and registration |
| `sanka_extensions.flow` | Declarative business requests with change-preservation and activation requirements |
| `sanka_extensions.code` | Typed requests and responses for code migration |

```python
from sanka_extensions.data import DataReader, DataWriter
from sanka_extensions.code import ExtensionRequest, ExtensionResponse
from sanka_extensions import flow

crm = flow.create(type="crm")
```

Flow's `create` constructs an unresolved definition without modifying a data endpoint.
The source SDK includes its versioned contract; CRM/billing packages and Flow
execution are not in the published marketplace. See [Flow contracts](docs/flow.md)
for reapplication, construction, verification and activation requirements.

See the [SDK guide](packages/sanka-extension-sdk/README.md) for development and the [compatibility guide](docs/naming-compatibility.md) for published package identifiers. `sanka-drf-replay` supplies optional request/response replay support for code extensions.

HubSpot, Salesforce, SendGrid, and other hosted SaaS implementations remain private Sanka API capabilities. Their data is accessed through hosted adapters; users do not install them as local extensions.

## Install and inspect

```bash
sanka extension marketplace add https://github.com/sankaHQ/extensions.git --name sanka --json
sanka extension marketplace add PATH_OR_GIT_URL --name third-party --trust --json
sanka extension add sanka/postgres --marketplace sanka --json
sanka extension list --json
```

CLI versions before the published-catalog default support follow the Git default
branch. For those versions, clone the published `extensions-v0.1.0a18` tag and add
that directory with `--trust`. On CLI versions supporting `--revision`, use the
full published commit `1a8c9450243b1cc25096396244dac5ed3cf7abfd` to pin this bundle.
Development `main` may reference wheels awaiting publication.

Installation makes a capability available. It does not authenticate any data endpoint or verify its reachability. Configure each data source or destination with its own endpoint and credential references before planning a data migration. Code extensions operate on projects and do not require a authenticated data endpoint.

## Catalog and manifests

`marketplace.json` uses `sanka-marketplace/v1`. Each manifest uses `sanka-extension-manifest/v2` and pins:

- extension ID and version;
- compatible `sanka-cli` versions;
- exact distribution identity and executable or data entry point;
- code lifecycle commands and project matching, or supported endpoint types and read/write roles;
- wheel filenames, immutable release URLs, and SHA-256 digests.

The published manifest values `kind="connector"` (Data) and `kind="migration"` (Code), the `providers` field, and `sanka.connectors` entry points remain wire compatibility contracts. Keep the two typed execution protocols distinct. See [the transition map](docs/naming-compatibility.md).

## Execution and trust

Sanka owns discovery, planning, execution, and verification. Extensions expose typed data readers and writers through the isolated extension host. Code extensions exchange validated JSON over standard input/output using `sanka-extension/v1`; diagnostics go to standard error.

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

`make build-release` builds and validates artifacts; it does not publish. After intentional wheel changes, run `make update-marketplace-hashes`, review the manifest diff, and validate again. Publish the SDK compatibility dependency, then the Sanka Extension SDK, then implementing extensions, and publish SDK changes before advancing runtime dependency pins. Release publication requires a reviewed release and separate authorization.

See [extension development](docs/extension-development.md) and [AGENTS.md](AGENTS.md) for ownership and licensing boundaries.

The converter benchmark gate pins a reviewed benchmark revision and covers DRF-to-FastAPI and DRF-to-Flask. Fully generated candidates must pass the generated-scope tests and independent benchmark gates. Partial candidates disclose route gaps. Private benchmark fixtures and reports remain outside this repository.

# Building Extensions

Extensions add data-access or code-conversion capabilities. They are installed independently of the shared Sanka runtime and are licensed Apache-2.0.
The SDK also provides the declarative business contract in `sanka_extensions.flow`.
See [Flow development](flow.md) for the supported source API and the runtime work
required before a business configuration can be applied.

## Start with a runnable example

The [Config upgrade starter](https://github.com/sankaHQ/sanka-examples/tree/main/extensions/config-upgrade)
shows a complete Code extension package, a manifest, SDK contract tests and
an acceptance check against published CLI `0.2.12` and SDK `0.1.0a4` wheels.
It scans a small JSON configuration and writes an upgrade plan. It deliberately
advertises only `scan` and `plan`; it does not claim to apply a migration.

Run its documented `check.py` command from an independent clone. It builds the
example wheel, creates isolated environments, verifies published SDK hashes,
adds an explicitly trusted local marketplace and invokes the public CLI. It also
checks invalid input and tampered wheel rejection. No runtime checkout or private
service is required. The macOS/Linux CI runs the same command.

For your own extension, replace the example namespace, distribution, executable,
project matcher and target, then implement and test your typed contract. Advertise
only commands you implement. Publish immutable HTTPS wheel URLs and all transitive
wheel hashes before sharing a marketplace. The local HTTPS fixture in the example
is a development harness, not a public distribution service.

The [SDK guide](sdk.md) installs the published
SDK in a Python 3.12+ development environment. The CLI stays in its own environment;
the runtime creates another isolated environment for the extension. Avoid imports
from the runtime and never edit its cache or project lock to simulate installation.

## Data access

```python
from sanka_extensions.data import ExtensionRegistration, DataReader, DataWriter


# The implementations satisfy the typed protocols and receive credentials per call.
def register(name: str, reader: DataReader, writer: DataWriter):
    return ExtensionRegistration(name=name, source=reader, destination=writer)
```

A registration declares a endpoint-type key and optional source/destination roles. A reader/writer implementation is reusable across configured data endpoints; do not store one account's credentials in a shared registration. Installing the extension does not authenticate a data endpoint.

Keep base protocols and optional capabilities separate. Implement `DataReader` and/or `DataWriter` and the capabilities the endpoint supports. Destination writes must require the complete non-null identity tuple when a route declares identity fields; never silently weaken a composite key.

- Import `sanka_extensions.data` and the extension's own third-party driver dependencies. Never import `sanka`, `sanka.runtime`, or another extension.
- Keep the SDK free of drivers and runtime dependencies except its typed compatibility package and every source file marked `SPDX-License-Identifier: Apache-2.0`.
- Export `EXTENSION`. Retain `CONNECTOR = EXTENSION` while published entry points target that compatibility name.
- Keep immutable distribution/module/entry-point identifiers listed in [naming-compatibility.md](naming-compatibility.md).
- Reject invalid capabilities and identities explicitly; do not add arbitrary in-process hooks.

HubSpot, Salesforce, SendGrid, and other hosted SaaS implementations belong in the private Sanka API/jobs runtime. Their credentials, clients, and registrations must not be published here.

## Code conversion

Use `sanka_extensions.code.ExtensionRequest` and `ExtensionResponse` for the code migration lifecycle. Requests and responses use the versioned `sanka-extension/v1` JSON contract; diagnostics belong on standard error. See the [SDK guide](sdk.md).

## Code extension protocol

Code extensions exchange validated JSON with the runtime over standard input and
output using the `sanka-extension/v1` protocol. A request contains:

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

Error responses include `error { code, message, details }`. The runtime checks
request identity, command, extension identity, artifact paths, and the complete
response shape; missing or extra fields fail the exchange.

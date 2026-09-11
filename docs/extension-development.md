# Building Extensions

Extensions add system-access or code-conversion capabilities. They are installed independently of the shared Sanka runtime and are licensed Apache-2.0.

## System access

```python
from sanka_extensions.systems import ExtensionRegistration, SystemReader, SystemWriter


# The implementations satisfy the typed protocols and receive credentials per call.
def register(name: str, reader: SystemReader, writer: SystemWriter):
    return ExtensionRegistration(name=name, source=reader, destination=writer)
```

A registration declares a system-type key and optional source/destination roles. A reader/writer implementation is reusable across configured systems; do not store one account's credentials in a shared registration. Installing the extension does not authenticate a system.

Keep base protocols and optional capabilities separate. Implement `SystemReader` and/or `SystemWriter` and the capabilities the system supports. Destination writes must require the complete non-null identity tuple when a route declares identity fields; never silently weaken a composite key.

- Import `sanka_extensions.systems` and the extension's own third-party driver dependencies. Never import `sanka`, `sanka.runtime`, or another extension.
- Keep the SDK free of drivers and runtime dependencies except its typed compatibility package and every source file marked `SPDX-License-Identifier: Apache-2.0`.
- Export `EXTENSION`. Retain `CONNECTOR = EXTENSION` while published entry points target that compatibility name.
- Keep immutable distribution/module/entry-point identifiers listed in [naming-compatibility.md](naming-compatibility.md).
- Reject invalid capabilities and identities explicitly; do not add arbitrary in-process hooks.

HubSpot, Salesforce, SendGrid, and other hosted SaaS implementations belong in the private Sanka API/jobs runtime. Their credentials, clients, and registrations must not be published here.

## Code conversion

Use `sanka_extensions.code.ExtensionRequest` and `ExtensionResponse` for the code migration lifecycle. Requests and responses use the versioned `sanka-extension/v1` JSON contract; diagnostics belong on standard error. See the [SDK guide](../packages/sanka-extension-sdk/README.md).

# Sanka Data Extension SDK

Dependency-free Apache-2.0 system access interfaces used by Sanka data extensions.

```python
from sanka_data import DataExtensionRegistration, SystemReader, SystemWriter
```

A data extension supplies readers, writers, and optional capabilities for a system type. Credentials are passed per operation, keeping configured systems independent. Installing an extension does not authenticate a database or account.

The SDK imports neither the Sanka runtime nor system implementations. Destination writers must require the complete non-null identity tuple when a route declares identity fields.

The distribution name `sanka-connector-sdk`, the `sanka.connectors` entry-point group, and the `sanka_connector` import aliases remain published compatibility contracts. Old type imports resolve to the same canonical classes. See [the transition map](https://github.com/sankaHQ/extensions/blob/main/docs/naming-compatibility.md).

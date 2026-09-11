# Sanka Extension SDK

Apache-2.0 interfaces for building Sanka Extensions with Python 3.12+.

```bash
pip install sanka-extension-sdk
```

```python
from sanka_extensions.systems import ExtensionRegistration, SystemReader, SystemWriter
from sanka_extensions.code import ExtensionRequest, ExtensionResponse
```

`systems` defines typed system access, records, capabilities, credentials, and registration. `code` defines validated lifecycle requests and responses for application conversion. They share one SDK and retain distinct execution contracts.

A system reader or writer receives credentials per operation. A code extension exchanges validated JSON with the runtime over standard input/output. Installation does not authenticate a system.

The SDK has no database drivers, framework runtimes, provider clients, or dependency on the Sanka runtime. Its only dependency is the lightweight published compatibility SDK. See [system access development](../../docs/extension-development.md) and [published compatibility contracts](../../docs/naming-compatibility.md).

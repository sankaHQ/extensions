# Sanka Extension SDK

Apache-2.0 interfaces for building Sanka Extensions with Python 3.12+.

```bash
pip install sanka-extension-sdk
```

```python
from sanka_extensions.systems import ExtensionRegistration, SystemReader, SystemWriter
from sanka_extensions.code import ExtensionRequest, ExtensionResponse
from sanka_extensions import flow

crm = flow.create(type="crm", parameters={"language": "ja"})
```

`systems` defines typed system access, records, capabilities, credentials, and registration. `flow` defines unresolved business-construction requests. `code` defines validated lifecycle requests and responses for application conversion. They share one SDK and retain distinct contracts.

Flow definitions are immutable and serialize with `flow.encode_definition`;
`flow.decode_definition` rejects unsupported schemas and weakened policies.
`create` does not load a template, make API calls or activate automations. A runtime
must implement the [Flow contract](../../docs/flow.md) before accepting a request.
The current CLI and Setup Wizard do not yet execute this new contract.

A system reader or writer receives credentials per operation. A code extension exchanges validated JSON with the runtime over standard input/output. Installation does not authenticate a system.

The SDK has no database drivers, framework runtimes, provider clients, or dependency on the Sanka runtime. Its only dependency is the lightweight published compatibility SDK. See [system access development](../../docs/extension-development.md) and [published compatibility contracts](../../docs/naming-compatibility.md).

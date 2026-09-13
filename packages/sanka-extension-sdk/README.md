# Sanka Extension SDK

Apache-2.0 interfaces for building Sanka Extensions with Python 3.12+.

```bash
python -m pip install \
  https://github.com/sankaHQ/extensions/releases/download/extensions-v0.1.0a18/sanka_connector_sdk-0.1.0a12-py3-none-any.whl \
  https://github.com/sankaHQ/extensions/releases/download/extensions-v0.1.0a18/sanka_extension_sdk-0.1.0a2-py3-none-any.whl
```

This installs released SDK `0.1.0a2`. The v2 Flow contract in this checkout is an
unpublished `0.1.0a3` candidate; use the repository uv workspace to develop it.

```python
from sanka_extensions.data import ExtensionRegistration, DataReader, DataWriter
from sanka_extensions.code import ExtensionRequest, ExtensionResponse
from sanka_extensions import flow

crm = flow.create(type="crm", parameters={"language": "ja"})
```

`data` defines typed data access, records, capabilities, credentials, and registration. `flow` defines unresolved business-construction requests. `code` defines validated lifecycle requests and responses for application conversion. They share one SDK and retain distinct contracts.

Flow definitions are immutable and serialize with `flow.encode_definition`;
`flow.decode_definition` rejects unsupported schemas and weakened policies.
`create` does not load a template, make API calls or activate automations. A runtime
must implement the [Flow contract](../../docs/flow.md) before accepting a request.
The current CLI and Setup Wizard do not yet execute this new contract.

A data reader or writer receives credentials per operation. A code extension exchanges validated JSON with the runtime over standard input/output. Installation does not authenticate a data endpoint.

The SDK has no database drivers, framework runtimes, provider clients, or dependency on the Sanka runtime. Its only dependency is the lightweight published compatibility SDK. See [data access development](../../docs/extension-development.md) and [published compatibility contracts](../../docs/naming-compatibility.md).

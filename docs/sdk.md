# Sanka Extension SDK

Apache-2.0 interfaces for building Sanka Extensions with Python 3.12+.
Install [uv](https://docs.astral.sh/uv/getting-started/installation/) before
running these macOS/Linux shell commands.

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install \
  https://github.com/sankaHQ/extensions/releases/download/sdk-v0.1.0a7/sanka_connector_sdk-0.1.0a12-py3-none-any.whl \
  https://github.com/sankaHQ/extensions/releases/download/sdk-v0.1.0a7/sanka_extension_sdk-0.1.0a7-py3-none-any.whl
```

This installs published SDK `0.1.0a7` and its exact compatibility dependency.
Use a Python 3.12+ virtual environment for extension development; do not install
the SDK into the CLI tool environment. CLI `0.2.12` is a verified consumer of
this SDK. SDK availability does not imply native Flow execution.

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
must implement the [Flow contract](flow.md) before accepting a request.
The SDK contract alone does not establish native workflow execution. See the
[CLI compatibility table](https://github.com/sankaHQ/sanka/blob/main/docs/compatibility.md)
for the tested runtime combination.

A data reader or writer receives credentials per operation. A code extension exchanges validated JSON with the runtime over standard input/output. Installation does not authenticate a data endpoint.

The SDK has no database drivers, framework runtimes, provider clients, or dependency on the Sanka runtime. Its only dependency is the lightweight published compatibility SDK. See [data access development](extension-development.md) and [published compatibility contracts](naming-compatibility.md).

The README inside an already published SDK wheel is release metadata. This guide
owns current installation instructions without changing those immutable bytes.

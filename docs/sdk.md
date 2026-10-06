# Sanka Extension SDK

Apache-2.0 interfaces for building Sanka Extensions with Python 3.12+.
Install [uv](https://docs.astral.sh/uv/getting-started/installation/) before
running these macOS/Linux shell commands.

Install the published SDK `0.1.0a8` separately from the CLI:

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install \
  'https://github.com/sankaHQ/extensions/releases/download/sdk-v0.1.0a8/sanka_connector_sdk-0.1.0a12-py3-none-any.whl#sha256=34da5c35aaa60fc19258e76b72a3eca58bf52fff96e2ccf9a0aa1115f8878d8e' \
  'https://github.com/sankaHQ/extensions/releases/download/sdk-v0.1.0a8/sanka_extension_sdk-0.1.0a8-py3-none-any.whl#sha256=faa18386a9376e7477f6465f56de106111071c84c4f6e5d65dd273b988cf3377'
```

Both wheels are published in `sdk-v0.1.0a8`; the URL fragments verify their
SHA-256 digests. Use a Python 3.12+ virtual environment for extension development;
do not replace the SDK embedded in the CLI tool environment. Standalone and
embedded SDK versions have separate compatibility requirements. SDK availability
does not imply native Flow execution.

For local SDK development, build this checkout with
`uv build --wheel --package sanka-extension-sdk --out-dir dist/sdk`, then install
the resulting wheel in your development environment.

```python
from sanka_extensions.app import ExtensionRegistration, DataReader, DataWriter
from sanka_extensions.code import ExtensionRequest, ExtensionResponse
from sanka_extensions import flow

crm = flow.create(type="crm", parameters={"language": "ja"})
```

`app` defines typed application data access, records, capabilities, credentials,
and registration. `flow` defines unresolved business-construction requests. `code`
defines validated lifecycle requests and responses for application conversion.
They share one SDK and retain distinct contracts.

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

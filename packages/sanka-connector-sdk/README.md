# Sanka Connector SDK

The dependency-free Apache-2.0 interface used by Sanka connector plugins.

```python
from sanka_connector import ConnectorRegistration, SourceConnector
```

Providers register through the `sanka.connectors` entry-point group. The SDK
does not import the Sanka migration runtime or any provider implementation.
When a route declares identity fields, destination implementations must require
the complete, non-NULL identity tuple for every record; they must never weaken
a composite identity to the subset supplied by one record.

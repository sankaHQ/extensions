# Sanka connector interface

The Apache-2.0 interface package used by
[Sanka](https://github.com/sankaHQ/sanka) migration connectors: connector
protocols, capability declarations, record and schema types, the
credential-provider protocol, registration, and the structured error taxonomy.

Connector source imports **this interface only** — never the AGPL-licensed
runtime modules — so the package and source-level license boundaries remain
explicit. CI enforces that boundary. The SDK is published separately as
`sanka-connector-sdk` for first-party and third-party connector authors.

**Status: pre-release, SPI v1 in place** — ported from Sanka's production
migration adapters: base `SourceConnector` / `DestinationConnector` protocols,
optional capability protocols (identity inspection, snapshot bounds, record
counts, owner directory, batch writes, schema provisioning, retry metrics,
limits, config validation), credentials + provider protocol, schema/record/
provisioning types, and the structured error taxonomy. Shapes may still move
before 0.1.0.

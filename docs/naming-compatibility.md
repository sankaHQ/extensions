# Extension and system naming transition

The same words apply to UI, developer documentation, and implementation. Extensions are capability packages; systems are the databases/accounts they operate on. Data and Code describe extension capabilities. “Connected” is a verified system status, never an installation status. Do not introduce Connections or Integrations as competing resource categories.

The shared `sanka` executable supports Sanka (system migrations), Sanka Flow (workflow migrations), and Sanka Code (code migrations). Technology names do not determine product ownership: moving PostgreSQL records is system migration; adapting application SQL/ORM is code migration.

## Canonical developer interface

| Previous name | Canonical name |
| --- | --- |
| `sanka_connector` SDK imports | `sanka_data` |
| `SourceConnector` | `SystemReader` |
| `DestinationConnector` | `SystemWriter` |
| `ConnectorRegistration` | `DataExtensionRegistration` |
| `ConnectorError` | `SystemAccessError` |
| `ProviderIdentity` | `SystemIdentity` |
| `ProviderTimeoutError` | `SystemTimeoutError` |
| `TransientProviderError` | `TransientSystemError` |
| `CONNECTOR` registration constant | `DATA_EXTENSION` |

The canonical `sanka_data` facade and old Python imports resolve to the same classes. Implementation storage remains under the published SDK module path during the transition, preserving identity even when an older separately installed SDK is present. There is no second SDK implementation. Preserve class identity and runtime capability checks across both paths. The runtime-owned registry is `DataExtensionRegistry`; configured endpoint descriptors are `SystemConfig`.

## Published compatibility contracts

| Contract retained | Consumer / reason | Removal condition |
| --- | --- | --- |
| `sanka-connector-sdk`, `sanka-connector-*` distributions and `sanka_connector_*` module paths | Immutable marketplace wheels and existing Python installations | A coordinated package release, migrated manifests, and tested rollback paths |
| `sanka_connector` and its public submodules / old exported type names | Existing extension wheels and private cloud bridges | All supported consumers move to `sanka_data`; remove only in a documented incompatible SDK release |
| `CONNECTOR` constant and `sanka.connectors` entry-point group | Existing manifests and hosts discover the published target | Versioned discovery transition with old-wheel acceptance tests |
| Manifest `kind="connector"`, `providers`, `entry_point`; `kind="migration"` | Existing manifest parsers and immutable project locks | New schema with explicit dual-reader migration and retained old-lock support |
| `sanka-connector/v1` and wire tag `ProviderIdentity` | Data host/client messages | A separately versioned protocol transition, never a Python-only rename |
| Public API `/v2/migrate/connectors` and cloud bridge import paths | Existing API clients and pinned private runtime | Reviewed API/client transition; no cloud dependency upgrade in this change |

These are tracked compatibility surfaces, not recommended names for new abstractions. `scripts/check_terminology.py` rejects new legacy SDK imports and type definitions outside compatibility modules. Ordinary network connections, database connection pools, credential providers, and third-party library terms retain their technical meaning.

SDK release order: Data Extension SDK, implementing data extensions, then runtime dependency updates. Marketplace and package publication remain separate from preparing and reviewing this source change.

# Data, Workflow and Code naming transition

Products describe what moves: data, workflows or code. Extensions are capability packages; data sources and destinations are configured endpoints such as databases, files and SaaS accounts. Installation does not establish authentication or reachability. "System" is no longer the product category or a canonical SDK namespace.

The shared `sanka` executable supports Sanka (data migrations), Sanka Flow (workflow migrations), and Sanka Code (code migrations). Technology names do not determine product ownership: moving PostgreSQL records is data migration; adapting application SQL/ORM is code migration.

## Canonical developer interface

| Previous name | Canonical name |
| --- | --- |
| `sanka_extension_sdk` code lifecycle imports | `sanka_extensions.code` |
| `sanka_connector` / `sanka_extensions.systems` SDK imports | `sanka_extensions.data` |
| `SourceConnector` / `SystemReader` | `DataReader` |
| `DestinationConnector` / `SystemWriter` | `DataWriter` |
| `ConnectorRegistration` | `ExtensionRegistration` |
| `ConnectorError` / `SystemAccessError` | `DataAccessError` |
| `ProviderIdentity` / `SystemIdentity` | `DataIdentity` |
| `ProviderTimeoutError` / `SystemTimeoutError` | `DataTimeoutError` |
| `TransientProviderError` / `TransientSystemError` | `TransientDataError` |
| `CONNECTOR` registration constant | `EXTENSION` |

The canonical `sanka_extensions.data` facade and old Python imports resolve to the same classes. Implementation storage remains under the published SDK module path during the transition, preserving identity even when an older separately installed SDK is present. The data protocols and code contract each have one shared implementation. Preserve class identity and runtime capability checks across all import paths. The runtime-owned registry is `ExtensionRegistry`; configured endpoint descriptors are `DataEndpoint` in the shared runtime.

The public SDK is named **Sanka Extension SDK**, with one `sanka_extensions` namespace. The standalone `sanka_data` namespace was never released. The selected API is `sanka_extensions.data`; it remains part of the one SDK alongside `.flow` and `.code`. The unified SDK owns `sanka_extensions` and the published code-contract module; its data facade uses the separately owned compatibility package so wheels do not overwrite each other's files.

The added `sanka_extensions.flow` namespace defines business-construction requests.
The earlier `sanka_extensions.blueprints` suggestion was never implemented or
published and needs no compatibility alias. `Blueprint` remains a name for Flow's
resolved configuration artifact. See [the Flow contract](flow.md) for source API
availability and the separate runtime implementation requirements.

## Published compatibility contracts

| Contract retained | Consumer / reason | Removal condition |
| --- | --- | --- |
| `sanka-connector-sdk`, `sanka-connector-*` distributions and `sanka_connector_*` module paths | Immutable marketplace wheels and existing Python installations | A coordinated package release, migrated manifests, and tested rollback paths |
| `sanka_connector` and its public submodules / old exported type names | Existing extension wheels and private cloud bridges | All supported consumers move to `sanka_extensions.data`; remove only in a documented incompatible SDK release |
| `sanka_extensions.systems` and its submodules / `SystemReader`, `SystemWriter`, identity and error names | Earlier source consumers remain compatible aliases of `.data` | Retain during migration; remove only through an explicit incompatible API transition |
| `sanka_extension_sdk` and `sanka_extension_sdk.contract` | Published code-extension imports | Migrate consumers to `sanka_extensions.code` before a documented incompatible release |
| `CONNECTOR` constant and `sanka.connectors` entry-point group | Existing manifests and hosts discover the published target | Versioned discovery transition with old-wheel acceptance tests |
| Manifest `kind="connector"`, `providers`, `entry_point`; `kind="migration"` | Existing manifest parsers and immutable project locks | New schema with explicit dual-reader migration and retained old-lock support |
| `sanka-connector/v1` and wire tag `ProviderIdentity` | Data-access host/client messages | A separately versioned protocol transition, never a Python-only rename |
| Public API `/v2/migrate/connectors` and cloud bridge import paths | Existing API clients and pinned private runtime | Reviewed API/client transition; no cloud dependency upgrade in this change |

These are tracked compatibility surfaces, not recommended names for new abstractions. `scripts/check_terminology.py` rejects new legacy SDK imports and type definitions outside compatibility modules. Ordinary network connections, database connection pools, credential providers, and third-party library terms retain their technical meaning.

SDK release order: `sanka-connector-sdk` compatibility dependency, `sanka-extension-sdk`, implementing extensions, then runtime dependency updates. Marketplace and package publication remain separate from preparing and reviewing this source change.

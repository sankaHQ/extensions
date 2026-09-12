# The Sanka extension model

Sanka Extensions is the permissively licensed extension layer for stack-specific
migration knowledge. The Sanka runtime remains the single lifecycle engine;
extensions contribute narrow typed capabilities without importing or duplicating
the engine.

## Extension families

Extensions contribute capabilities such as:

- **Framework extensions** detect and inspect frameworks such as Django or Flask
  and provide typed conversion lifecycles.
- **Database extensions** inspect or write databases such as PostgreSQL, SQLite, and
  ClickHouse.
- **Language and library extensions** contribute bounded analysis or
  transformation capabilities for one ecosystem.
- **File extensions** read or write formats such as CSV and Markdown inside a
  reviewed root.
- **Business extensions** will provide reusable CRM, billing and other business
  configurations. Their declarative SDK contract is `sanka_extensions.flow`;
  executable packages and runtime dispatch remain future work.

The Sanka Extension SDK provides one `sanka_extensions` namespace. Use
`sanka_extensions.data` for data readers, writers, and registration;
use `sanka_extensions.code` for typed code-migration requests and responses.
Use `sanka_extensions.flow` for the [declarative Flow contract](flow.md).
Both interfaces are implemented, including executable PostgreSQL, SQLite, CSV,
Markdown, ClickHouse, DRF-to-FastAPI, and DRF-to-Flask extensions. See the
[generated catalog](catalog.md) and [development guide](extension-development.md).
New capabilities require reviewed, typed contracts and boundary validation.
Published identifiers are documented in the [compatibility guide](naming-compatibility.md).

## Resolver direction

The runtime does not currently download arbitrary community packages during
`scan` or `plan`. The intended resolver keeps that convenience deterministic:

1. Core performs a shallow, dependency-free fingerprint of the project.
2. It resolves only matching extension metadata from a reviewed catalog.
3. Policy decides whether network access and materialization are allowed.
4. Exact distributions run from an isolated environment, never by mutating the
   base Sanka installation.
5. The plan records every extension name, version, artifact hash, capability,
   and configuration digest so later phases reproduce the reviewed result.

Offline mode must operate from an existing lock and cache. A missing or
untrusted extension fails with an actionable diagnostic; Sanka must not silently
fall back to a different implementation. Pull requests can propose extensions,
but a merge does not automatically add a package to the reviewed resolver
catalog.

## Runtime boundary

Extensions may depend on their own framework or driver libraries and on a
permissive Sanka SDK. They must not import `sanka`, `sanka.runtime`, another
extension, hosted Sanka code, or private credentials. Interfaces use validated serializable
inputs and outputs, declare capabilities explicitly, and fail closed when a
requested capability is unsupported.

Hosted SaaS and managed-data migrations remain Sanka API capabilities. Their
credentials, long-running jobs, rate controls, and audit evidence do not belong
in local extensions.

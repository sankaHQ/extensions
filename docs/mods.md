# The Sanka mod model

Sanka Mods is the permissively licensed extension layer for stack-specific
migration knowledge. The Sanka runtime remains the single lifecycle engine;
mods contribute narrow typed capabilities without importing or duplicating the
engine.

## Mod families

The repository is intended to support these families:

- **Framework mods** detect and inspect frameworks such as Django or Flask and
  can later expose typed generation strategies.
- **Database mods** inspect or write databases such as PostgreSQL, SQLite, and
  ClickHouse.
- **Language and library mods** contribute bounded analysis or transformation
  capabilities for one ecosystem.
- **File mods** read or write formats such as CSV and Markdown inside a reviewed
  root.

The Connector SDK is the first implemented mod interface. Its existing
`sanka-connector-*` distributions, `sanka_connector` import, and
`sanka.connectors` entry-point group remain compatibility contracts. New mod
families require a reviewed, typed SDK interface before executable packages are
accepted; repository membership alone is not an execution contract.

## Resolver direction

The runtime does not currently download arbitrary community packages during
`scan` or `plan`. The intended resolver keeps that convenience deterministic:

1. Core performs a shallow, dependency-free fingerprint of the project.
2. It resolves only matching mod metadata from a reviewed catalog.
3. Policy decides whether network access and materialization are allowed.
4. Exact distributions run from an isolated environment, never by mutating the
   base Sanka installation.
5. The plan records every mod name, version, artifact hash, capability, and
   configuration digest so later phases reproduce the reviewed result.

Offline mode must operate from an existing lock and cache. A missing or
untrusted mod fails with an actionable diagnostic; Sanka must not silently fall
back to a different implementation. Pull requests can propose mods, but a merge
does not automatically add a package to the reviewed resolver catalog.

## Runtime boundary

Mods may depend on their own framework or driver libraries and on a permissive
Sanka SDK. They must not import `sanka`, `sanka.runtime`, another mod, hosted
Sanka code, or private credentials. Interfaces use validated serializable
inputs and outputs, declare capabilities explicitly, and fail closed when a
requested capability is unsupported.

Hosted SaaS and managed-system migrations remain Sanka API capabilities. Their
credentials, long-running jobs, rate controls, and audit evidence do not belong
in local mods.

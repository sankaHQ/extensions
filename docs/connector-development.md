# Sanka connectors

First-party connectors live under `packages/`, one distribution per provider,
and are licensed **Apache-2.0**. Each provider is installed independently from
the Sanka migration runtime.

Rules:

- A connector imports **only** `sanka_connector` (the Apache-2.0 SDK) — never
  `sanka.runtime`, `sanka.cli`, or any other runtime module. CI enforces this
  (`scripts/check_import_boundaries.py`), which is what keeps connectors from
  becoming derivative works of the AGPL runtime.
- Every source file carries `# SPDX-License-Identifier: Apache-2.0`.
- One package, documentation file, and test directory per provider; each
  provider distribution owns its `sanka.connectors` entry point.

First-party local providers: `markdown`, `csv`, `sqlite`, `postgres`, and
`clickhouse`.

SaaS and managed-system providers such as HubSpot, Salesforce, and SendGrid do
not belong in this repository. They run inside Sanka's hosted API and job
runtime so credentials, execution controls, and audit evidence stay managed by
Sanka.

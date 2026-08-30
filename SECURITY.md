# Security Policy

## Supported versions

Sanka Connectors is currently pre-1.0. Security fixes are provided for the
latest published prerelease only. Older prereleases do not receive security
backports.

## Reporting a vulnerability

Do not open a public GitHub issue for a suspected vulnerability.

Email `hey@sanka.com` with a subject beginning
`[Security][sanka-connectors]`. Include the affected package and version,
reproduction steps, expected impact, and any suggested mitigation. Do not
include real customer data, access tokens, passwords, or other credentials.

We will use the reporting channel to coordinate validation, remediation, and
responsible disclosure. No response or resolution time is guaranteed by this
policy.

## Security boundaries

This repository contains the Connector SDK and local or offline connector
packages. Credentials and endpoint configuration are trusted operator inputs;
source records, file trees, schema names, and database contents are untrusted.

Configured source roots, reviewed filters, record identities, and destination
identifiers are enforced scope boundaries. Unsupported filters must fail
closed, files must remain inside their configured roots, and distinct source
names must never be silently merged.

Hosted SaaS and managed-system connectors are private Sanka API capabilities
and are outside this repository.

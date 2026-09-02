# Sanka DRF-to-FastAPI extension

This independently executable extension scans Django REST Framework projects and plans,
generates, tests, and verifies FastAPI migrations through the `sanka-extension/v1` JSON
subprocess contract.

It writes one JSON response to stdout. Diagnostics are written to stderr.

## Parity notes

Every scanned route carries `parity_notes`: facts about the exact behavior of the
source application that a port must reproduce, derived from the live Django and DRF
classes rather than guessed. Families: `routing` (allowed methods, 405/OPTIONS bodies,
missing-object and trailing-slash behavior), `auth` (authenticator order, 401/403/404
ordering, exact token/session/basic failure details, CSRF), `conditional` (ETag and
304/412 logic and the operations that call it), `pagination`, `ordering`, `filtering`,
`multipart` (parsers and file-field rules), `uniqueness`, `nullability`, `messages`
(exact per-field validation strings), and `overrides` (project methods and the literal
strings they emit). Notes appear in `.sanka/scan.json` (schema 5), `plan-*.json`
(schema 4), the gap report, and the generated manifest's `unsupported_routes`. A family
that cannot be derived reports `SANKA_DRF_PARITY_UNAVAILABLE` instead of failing the scan.

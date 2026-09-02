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

## OPTIONS and 405 parity

Generated native apps answer `OPTIONS` with the exact `SimpleMetadata` body DRF would
send — view name, description, renderer and parser media types, and the `actions`
field map for POST/PUT when the caller passes the permission checks (PUT only when the
object exists and, for owner-restricted views, belongs to the caller). The scan captures
the anonymous and the authorized variants from the installed DRF; the runtime chooses
between them per request. Unsupported methods answer DRF's `405 {"detail": "Method
\"X\" not allowed."}` with the `Allow` header in `http_method_names` order.

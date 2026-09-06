# Sanka DRF replay

Shared standard-library replay engine for the DRF-to-FastAPI and DRF-to-Flask
extensions. It is a helper library, not an executable extension or an SDK.
Frameworks run in the source and candidate interpreters, never as dependencies
of this package. Each scenario gets separate copies of a freshly seeded SQLite
database. The configured database environment variable must select that database.

Scenario files contain a non-empty JSON array with unique `id`, `method`, `path`,
optional `headers`, `capture_headers`, `setup`, and either `body`, `body_base64`,
or `multipart`. Omitting `body` sends no bytes; `body: null` sends JSON null.
`body_base64` supports malformed JSON and other raw requests. Header names are
case insensitive. Additional scenarios and seeds are caller-owned inputs.

Top-level scenarios may declare `expected_source_status` (an integer HTTP status).
Replay fails if the source returns a different status, even when the candidate
matches it. For example, `{"id":"read","method":"GET","path":"/items/1/",
"expected_source_status":200}` requires the seeded record and valid authentication;
matching 404s cannot satisfy it. Setup requests do not support this assertion.

With `--edge-probes`, extensions reuse one matching scenario's path, headers,
captured response headers and setup per scanned route. Declared success cases
take priority, then requests with headers. This adds authenticated OPTIONS and
HEAD checks without moving credentials between unrelated routes. Use explicit
scenarios for additional tenants, roles and request contexts. Generated requests
do not inherit the original expected status, since their methods differ.

Reports retain every scenario. The extension response contains counts, up to
20 failing scenario descriptions, and the report path. Replay is a development
check; it does not replace the independent benchmark's native compliance gate.

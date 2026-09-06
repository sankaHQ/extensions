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

Reports retain every scenario. The extension response contains counts, up to
20 failing scenario descriptions, and the report path. Replay is a development
check; it does not replace the independent benchmark's native compliance gate.

# Sanka DRF replay

Shared standard-library replay engine for the DRF-to-FastAPI and DRF-to-Flask
extensions. It is a helper library, not an executable extension or an SDK.
Frameworks run in the source and candidate interpreters, never as dependencies
of this package. Each scenario gets separate copies of a freshly seeded SQLite
database. The configured database environment variable must select that database.

Local media is also isolated: preparation and each source/candidate replay receive
separate copies of the seeded `MEDIA_ROOT`. Serving settings should inherit the
source setting or honor `BENCH_MEDIA_ROOT`. Seeds must write beneath
`settings.MEDIA_ROOT`, without assigning a new directory. Replay compares relative
file names and SHA-256 content after setup and the final request; media differences
fail verification even if responses and databases match. This covers local media,
not remote storage or arbitrary external side effects. Media symlinks are rejected.
Multipart requests preserve the caller's explicit boundary and binary bytes.

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

Scan-derived replay probes also check declared serializer read-only fields, malformed Token credentials, and missing session CSRF headers when caller-supplied requests provide that context. These bounded probes reuse source behavior as the reference; an intentional authentication rejection counts as coverage only after its original source request succeeds.

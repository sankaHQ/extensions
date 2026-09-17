# Sanka DRF replay

Shared standard-library replay engine for the DRF-to-FastAPI and DRF-to-Flask
extensions. It is a helper library, not an executable extension or an SDK.
Frameworks run in the source and candidate interpreters, never as dependencies
of this package. SQLite is the default; each scenario gets separate copies of a
freshly seeded database. PostgreSQL is an explicit opt-in described below. The
configured database environment variable must select the allocated database.

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

Supplied write requests also receive binary and boundary-token upload probes and
duplicate-record probes for two collection depths in JSON objects. They preserve
the original route, fields, filenames, authentication and setup. Expected responses
come from executing the source, including its parser and validation behavior;
neither successful writes nor rejection statuses are assumed. All contract probes
share a 12-request cap. Use explicit scenarios for deeper or additional contexts.

## PostgreSQL replay

Set the extension verification configuration to:

```json
{
  "database_backend": "postgresql",
  "postgres_admin_dsn_env": "SANKA_REPLAY_POSTGRES_ADMIN_DSN",
  "db_env": "SANKA_TEST_DB",
  "candidate_db_env": "SANKA_TEST_DB",
  "scenarios": "scenarios.json",
  "seed": "seed.py",
  "candidate": ".sanka/flask"
}
```

With the CLI, pass those extra options through `--extension-config` and explicitly
forward the named environment variable with `--extension-env`:

```sh
sanka verify . --settings settings --scenarios scenarios.json --seed seed.py \
  --candidate .sanka/flask \
  --extension-config '{"database_backend":"postgresql","postgres_admin_dsn_env":"SANKA_REPLAY_POSTGRES_ADMIN_DSN"}' \
  --extension-env SANKA_REPLAY_POSTGRES_ADMIN_DSN
```

The named environment variable contains a PostgreSQL URL for a **dedicated test
service**, with explicit host, user, password and database, and permission to create databases.
Password-file, service-file and ambient libpq fallbacks are unsupported. Supply it through your execution
environment; do not put its value in project configuration or scenario files.
The source interpreter must provide psycopg 3. The helper itself remains
standard-library-only, and verification does not install dependencies.

Replay creates an empty database, runs source migrations and the supplied seed,
then clones that prepared database independently for each source and candidate
scenario. It never clones the configured application database. Clones preserve
constraints, data and sequence state. Snapshots compare database rows and sequence
state, including sequence changes caused by rolled-back writes. Setup requests
and the scenario request share a clone; subsequent scenarios start from the
original seed again.

Source settings must read `db_env` as a PostgreSQL URL (SQLite mode supplies a
filesystem path). A distinct `candidate_db_env` may be supplied for candidate
settings. Native Flask factories receive the allocated SQLAlchemy database URL.
Every supported connection must point at its allocated database; ignored or
incompatible settings fail verification. Application imports are trusted Python
execution, not sandboxed code.

Cleanup runs on success or failure, including when `keep_temp` preserves local
diagnostics. It checks a per-run ownership marker before terminating connections
or dropping a database; it never discovers databases by a shared prefix. If a
creation timeout leaves ownership unconfirmed, replay refuses deletion and reports
the database name for manual inspection rather than risking an unrelated database. Reports contain backend and environment-variable names, not
the maintenance URL. Missing permissions, incompatible settings or cleanup failures
are errors, not successful verification.

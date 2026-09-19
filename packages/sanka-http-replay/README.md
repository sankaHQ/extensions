# sanka-http-replay

Versioned HTTP scenario and observation contract shared by Sanka code extensions
(`sanka/typescript-to-rust` today; `sanka/python-to-golang` can adopt it for its
write replay). Standard library only. It owns:

- `sanka.http-scenarios/v1`: an **ordered** list of requests. `validate_scenarios`
  accepts `{"schema": "sanka.http-scenarios/v1", "scenarios": [...]}` or a bare
  list, and the hosted `sanka-verify.json` spelling (`expected_source_status` is
  an alias of `expected_status`; `setup` requests are rejected because the v1
  sequence is ordered, list them as earlier scenarios). Each scenario has `id`,
  `method`, `path`, optional `headers` (no content headers), optional JSON `body`
  (never on GET/HEAD/OPTIONS) and optional `expected_status`. At most 256
  scenarios, 64 KiB per body.
- `default_scenarios(operations, models)`: a deterministic sequence for captured
  single-table contracts. Operations are `{"method", "path", "kind", "model",
  "conflict"}` with kinds `literal`, `list`, `lookup`, `create`, `update`,
  `replace`, `delete`; models are `{"name", "table", "fields": [{"name", "type"
  (integer | bigint | boolean | string), "nullable", "primary_key", "auto",
  "unique"}]}`. The sequence covers invalid bodies, unknown keys, creation,
  duplicates (409 when the model has a UNIQUE column and the create operation
  declares `conflict`), lookups by id, partial and full updates with null and
  absent semantics, deletes twice, creation after deletion, and finally every
  literal or list GET route. It assumes both databases start at the captured
  baseline (empty captured tables with fresh identity sequences), so the first
  created row has id 1.
- `sanka.http-observations/v1`: what a runner records per scenario:
  `{"id", "method", "path", "status", "media_type", "body", "tables"?, "sequences"?}`.
  `body` is the parsed JSON body or `null` when empty; `media_type` is the
  `Content-Type` before `;`; `tables` maps every captured table to its rows
  (`SELECT <captured columns> FROM <table> ORDER BY <primary key>`) after the
  request; `sequences` maps tables with an auto primary key to
  `[last_value as a decimal string, is_called]` of `pg_get_serial_sequence`.
- `compare(scenarios, candidate, source)`: per-step problems (expected status,
  JSON media type or bodyless responses, and the first JSON-path difference
  between the source and the candidate observation) plus an overall `ok`.

## Runner contract

A runner is owned by its extension (a Node script for Express, a `cargo test`
probe for axum, a Go test for the Go targets). It reads a cases document
(`cases_document(scenarios)`), performs the requests **in order** against one
application instance with one database that starts at the captured baseline,
sends `body` as `application/json` when present plus the scenario `headers`,
never opens a TCP listener (in-process injection or a Unix domain socket), and
writes the observation list. Bigint values are serialized as decimal strings on
both sides (node-postgres and the generated Rust models agree). Runners must not
alter the candidate or the source, and extensions re-capture the source before
and after replay and discard observations on drift.

Resetting to the baseline is the extension's job: the generated target owns a
migration tool (`migrate down` then `up`), and the source side recreates the
captured tables from the captured `schema.sql` on a dedicated fixture database
that the operator names explicitly (never an ambient `DATABASE_URL`).

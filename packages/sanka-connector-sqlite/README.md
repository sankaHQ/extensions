# SQLite connector

SQLite as either side of a Sanka run.

As a **source**, every user table becomes a migratable object: fields and
types come from `PRAGMA table_info`, the identity is the table's
single-column primary key (or `rowid`, exposed as an extra field), and reads
use keyset pagination (`WHERE <pk> > ? ORDER BY <pk>`) for deterministic,
resumable cursors. Exact record counts are reported; `WITHOUT ROWID` tables
with composite primary keys are inventoried with a warning and no identity.

As a **destination**, records are written into one table per target object
(created on first write, columns added as new fields appear), with
identity-based upserts honoring the run's conflict policy and JSON-encoded
complex values. Inventory reads back table row counts for verification.

Apache-2.0; depends only on the `sanka-connector-sdk` interface (stdlib `sqlite3`).

Source filters are not supported yet and fail closed instead of being ignored.
Safe lowercase SQL identifiers are preserved; names requiring lossy
normalization receive a deterministic digest suffix so distinct fields or
object routes cannot silently collapse onto one target name.

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
Writable connections operate on a private working database. Each committed
result is copied through a bound parent descriptor and atomically published to
the destination; SQLite never opens the destination pathname or creates
sidecars beside it.

Apache-2.0; depends only on the `sanka-connector-sdk` interface (stdlib `sqlite3`).

Source filters are not supported yet and fail closed instead of being ignored.
Source databases are opened read-only. Symbolic links, hard-linked database
files, and non-regular files are rejected on both source and destination paths.
Ordinary safe lowercase SQL identifiers are preserved; lossy names and names
inside Sanka's reserved encoded namespace receive distinct deterministic
prefixes and digest suffixes. Declared composite identities must be complete
and non-NULL on every record. A declared `rowid` is never mistaken for SQLite's
hidden row identity.

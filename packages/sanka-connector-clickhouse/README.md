# ClickHouse connector

Writes Sanka records into a ClickHouse database as a migration
**destination**: one table per target object, created on first write with
column types inferred from first-seen values and widened with `ALTER TABLE …
ADD COLUMN IF NOT EXISTS` as new fields appear. Tables whose run declares
identity fields use `MergeTree ORDER BY (<identity columns>)`; repeated
identities remain separate rows. Only the `create` conflict policy is
supported. `skip_existing` and `update_existing` fail before connecting because
this connector cannot enforce them atomically. Connections accept `http://`, `https://`, or
`clickhouse://` URLs (the last treated as HTTP). Apache-2.0; depends only on
the `sanka-connector-sdk` interface and `clickhouse-connect`.

Ordinary safe lowercase SQL identifiers are preserved. Lossy names and names
inside Sanka's reserved encoded namespace receive distinct deterministic
prefixes and digest suffixes, preventing separate source names from silently
sharing one target name. Declared composite identities must be complete and
non-NULL on every record.

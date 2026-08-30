# CSV connector

Reads a single CSV or TSV file as a Sanka **source**: the header
row defines the fields, every data row becomes one record, and the sanitized
file stem is the object key. The delimiter is sniffed, falling back to `,`
(tab for `.tsv`). Inventory infers number/boolean column types while streaming
rows and warns on mixed-type columns; record values themselves are passed
through as strings. Duplicate headers after trimming and case-folding are
rejected rather than silently merged. A column named `id` (case-insensitive) is the
identity — otherwise a synthetic 1-based `row` field is exposed and used,
with a warning. Apache-2.0; depends only on the `sanka-connector-sdk` interface (stdlib
`csv`).

Source filters are not supported yet and fail closed instead of being ignored.
Symbolic links and non-regular input files are rejected. The default read
budget is 64 MiB and 1,000,000 data rows; trusted callers can lower or raise
those positive limits with `max_bytes` and `max_rows` connection settings.
CSV data is decoded and parsed as a bounded stream rather than loaded into
memory as one complete file.

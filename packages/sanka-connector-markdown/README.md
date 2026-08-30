# Markdown connector

Reads a directory of Markdown files as a Sanka **source**: YAML
frontmatter becomes structured fields, the body becomes `content`, the
relative path is the identity. Inventory reports the frontmatter field union
with inferred types and warns on mixed-type fields and unparseable
frontmatter. Apache-2.0; depends only on the `sanka-connector-sdk` interface (+ PyYAML).

Source filters are not supported yet and fail closed instead of being ignored.
Markdown files must remain inside the configured directory; symbolic-link
files are rejected rather than followed across that source boundary.
Duplicate YAML keys, keys that collide after string conversion, and
frontmatter keys that collide with `path`, `slug`, or `content` are rejected.
The default read budget is 100,000 Markdown files, 200,000 traversed directory
entries, 64 directory levels, and 8 MiB per file; trusted callers can set
positive `max_files`, `max_entries`, `max_depth`, and `max_file_bytes`
connection settings. Traversal is depth-first and keeps only the current
directory chain open.

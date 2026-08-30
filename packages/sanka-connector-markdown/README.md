# Markdown connector

Reads a directory of Markdown files as a Sanka **source**: YAML
frontmatter becomes structured fields, the body becomes `content`, the
relative path is the identity. Inventory reports the frontmatter field union
with inferred types and warns on mixed-type fields and unparseable
frontmatter. Apache-2.0; depends only on the `sanka-connector-sdk` interface (+ PyYAML).

Source filters are not supported yet and fail closed instead of being ignored.
Markdown files must remain inside the configured directory; symbolic-link
files are rejected rather than followed across that source boundary.

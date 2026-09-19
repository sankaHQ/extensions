# Experimental Go and Rust release

`api-converters-v0.1.0a1` is a scoped GitHub prerelease containing Python-to-Go
and TypeScript-to-Rust 0.1.0a1, TypeScript capture and HTTP replay helpers, and
the original published Extension SDK 0.1.0a4 and compatibility SDK 0.1.0a12 wheels.
It does not publish React Native, Compose, Jev, or a replacement SDK.

## Qualification

The dedicated `api-release.yml` workflow builds with exact build dependencies,
verifies every wheel against checked-in SHA-256 manifests, and runs the previously
reviewed examples from `sanka-examples` commit
`e4b9990ccc21ec3d1e775b0955f021f2083fc524`. The examples' source, plan review,
source-preservation, target compilation and actual HTTP comparison assertions
remain unchanged. Only their unpublished-candidate installer is replaced with
the built release wheels. CLI 0.2.12 and published SDK wheels run in isolated
consumer environments. Setup downloads public dependencies; migration uses local
synthetic sources and requires no credentials, private services or inference.

Go qualification uses Python 3.12 and Go 1.26.5 with the Flask/Fiber literal-GET
example. Rust qualification uses Node 22.14.0 and Rust 1.93.1 with the
Express/axum literal-GET example. Both run all five public CLI stages and require
explicit success. Broader package CI exercises additional supported framework
and database fixtures, but that is separate from the released example contract.

Local build and byte verification:

```bash
uv sync --frozen --all-packages
uv run python scripts/build_api_release.py
uv run python -m pytest tests/test_api_release.py tests/test_marketplace.py -q
```

`--write-manifests` is an intentional development operation after package changes.
Review the resulting digest changes before committing; CI never rewrites them.
Never replace artifacts or reuse a published version with different bytes.

## Publication and readback

After exact-head approval, green CI and governed landing, create the immutable
`api-converters-v0.1.0a1` tag at the reviewed merge commit. Dispatch
`api-release.yml` on that tag. The workflow verifies that the tag is contained in
main and repeats both consumer qualifications before its publication job runs.
The release contains six wheels, two manifests, a scoped catalog, and the two
acceptance reports. Existing marketplace releases are untouched.

After publication, download all assets into a new directory and run
`scripts/build_api_release.py --check-only --output-dir <download-directory>`
from the release commit. Then rerun each acceptance against the public Git catalog:

```bash
uv run python scripts/qualify_api_release.py \
  --examples release/examples --release release/downloaded-api-converters \
  --target go --report release/published-go.json \
  --published-revision <full-release-commit>
uv run python scripts/qualify_api_release.py \
  --examples release/examples --release release/downloaded-api-converters \
  --target rust --report release/published-rust.json \
  --published-revision <full-release-commit>
```

The examples directory must be a clean checkout of the pinned commit above.
Keep downloaded acceptance reports outside the nine-file artifact directory
when invoking the strict artifact validator. Public readback must pass before
describing publication as consumer-verified or updating examples to published
installation commands. The CLI default marketplace pin is intentionally unchanged.

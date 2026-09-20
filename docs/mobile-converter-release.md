# Experimental React Native release

`mobile-converters-v0.1.0a1` is a scoped GitHub prerelease containing React Native
to native 0.1.0a1, the TypeScript capture helper, and the original published
Extension SDK 0.1.0a4 and compatibility SDK 0.1.0a12 wheels. The helper and SDK
wheels are byte-identical to the `api-converters-v0.1.0a1` assets of the same name.
It does not publish Go, Rust, Jev, a Compose emitter, or a replacement SDK.

## Qualification

The dedicated `mobile-release.yml` workflow builds with exact build dependencies,
verifies every wheel against the checked-in SHA-256 manifest, and runs the previously
reviewed React Native task-list example from `sanka-examples` commit
`e4b9990ccc21ec3d1e775b0955f021f2083fc524`. The example's source checks, plan review,
source-preservation, generated-file and structural replay assertions remain
unchanged. Only its unpublished-candidate installer is replaced with the built
release wheels. CLI 0.2.12 and published SDK wheels run in isolated consumer
environments. Setup downloads public dependencies; migration uses the local synthetic
source and requires no credentials, private services or inference.

SwiftUI qualification runs on macOS 15 with Node 22.14.0 and the runner's default
Xcode: all five public CLI stages, the macOS package build and the structural tree
comparison of the five replay scenarios. Compose qualification runs on Ubuntu 24.04
with Node 22.14.0: scan and plan only, because no Compose emitter exists yet. Neither
path builds an iOS simulator or device app, executes the source beyond its Metro
bundle, or compares pixels, layout or fonts; `complete_app` stays `false`. Broader
package CI exercises additional fixtures, but that is separate from the released
example contract.

Local build and byte verification:

```bash
uv sync --frozen --all-packages
uv run python scripts/build_mobile_release.py
uv run python -m pytest tests/test_mobile_release.py tests/test_marketplace.py -q
```

`--write-manifests` is an intentional development operation after package changes.
Review the resulting digest changes before committing; CI never rewrites them.
Never replace artifacts or reuse a published version with different bytes. The
`sanka-ts-capture` wheel must keep the bytes published under `api-converters-v0.1.0a1`;
a changed helper needs a new helper version in both releases.

## Publication and readback

After exact-head approval, green CI and governed landing, create the immutable
`mobile-converters-v0.1.0a1` tag at the reviewed merge commit. Dispatch
`mobile-release.yml` on that tag. The workflow verifies that the tag is contained in
main and repeats both consumer qualifications before its publication job runs.
The release contains four wheels, one manifest, a scoped catalog, and the two
acceptance reports. Existing marketplace releases are untouched.

After publication, download all assets into a new directory and run
`scripts/build_mobile_release.py --check-only --output-dir <download-directory>`
from the release commit. Then rerun each acceptance against the public Git catalog:

```bash
uv run python scripts/qualify_mobile_release.py \
  --examples release/examples --release release/downloaded-mobile-converters \
  --target swiftui --report release/published-swiftui.json \
  --published-revision <full-release-commit>
uv run python scripts/qualify_mobile_release.py \
  --examples release/examples --release release/downloaded-mobile-converters \
  --target compose --report release/published-compose.json \
  --published-revision <full-release-commit>
```

The examples directory must be a clean checkout of the pinned commit above with
Node 22 first on `PATH`. The SwiftUI readback needs macOS; on a Mac whose Xcode
license is not accepted, export `DEVELOPER_DIR=/Library/Developer/CommandLineTools`
and the harness forwards it. Keep downloaded acceptance reports outside the six-file
artifact directory when invoking the strict artifact validator. Public readback must
pass before describing publication as consumer-verified or updating examples to
published installation commands. The CLI default marketplace pin is intentionally
unchanged.

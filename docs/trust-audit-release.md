# Trust audit release

The `trust-audit-v0.1.0a1` release is prepared separately from the broader extension
marketplace releases. It contains the `sanka/trust-audit` 0.1.0a1 wheel, the published
Extension SDK 0.1.0a4 and Connector SDK 0.1.0a12 wheels, a complete manifest, a scoped
catalog and public-CLI acceptance evidence.

`make build-trust-audit-release` builds only this closure with Hatchling's fixed
`SOURCE_DATE_EPOCH`, verifies every byte against the reviewed manifest, and validates
wheel dependencies, entry point and the plan settings declaration. After a source
change, bump the version and regenerate the manifest with
`scripts/build_trust_audit_release.py --write-manifest`.

## Release operator

1. Land the release PR after passing CI, including `Trust audit release qualification`.
2. Verify `trust-audit-v0.1.0a1` does not exist. Create its immutable tag at the landed
   release commit. Do not move a tag or replace assets after publication.
3. Dispatch `.github/workflows/trust-audit-release.yml` on that tag. It refuses a
   different tag or a commit outside `origin/main`, rebuilds and verifies the pinned
   closure, and runs `scripts/accept_trust_audit_release.py` with public CLI 0.3.11
   in an isolated environment before publishing. CI on pull requests never publishes.
4. Download the published catalog, manifest and three wheels and verify their hashes
   against the landed manifest. Register the official Git marketplace at the full
   release commit (`--revision`), install `sanka/trust-audit` from its published wheel
   URLs, and run scan, plan and apply on a scratch project.
5. Only then advance the CLI's official catalog pin to a commit that contains this
   marketplace entry.

The extension reads repository files only. Publication does not establish that
deployed binaries, built images or host trust stores are fixed.

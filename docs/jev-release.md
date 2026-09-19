# Jev converter release

The `llm-to-jev-v0.1.0a1` release is prepared separately from the broader extension
marketplace release. It contains the unchanged converter 0.1.0a1 wheel, published
Extension SDK 0.1.0a4 and Connector SDK 0.1.0a12 wheels, a complete manifest, a scoped
catalog and clean-consumer acceptance evidence. Provider SDKs are destination-owned.

Status: release preparation. Download URLs in the reviewed manifest become usable
only after publication and readback. Do not describe this source change as a
published package or advertise install commands before that verification.

`make build-jev-release` builds only this closure, verifies every byte against the
reviewed manifest, and validates wheel dependencies, entry point and required
template/schema data. The standard marketplace release remains on its existing
tag and existing package bytes. Its future catalog can reference this independent
Jev release once published.

## Release operator

1. Land the release PR through `sanka-pr-flow` after exact-head human approval and
   passing CI, including `Jev release qualification`.
2. Verify `llm-to-jev-v0.1.0a1` does not exist. Create its immutable tag at the landed
   release commit. Do not move a tag or replace assets after publication.
3. Dispatch `.github/workflows/jev-release.yml` on that tag. It refuses a different
   tag or a commit outside `origin/main`, rebuilds and verifies the pinned closure,
   and runs the reviewed cookbook with separate public CLI, extension and destination
   environments before publishing. CI on pull requests never publishes.
4. Download the actual release catalog, manifest and three wheels. Verify their
   hashes against the landed manifest and run the isolated cookbook acceptance
   again. Also register the official Git marketplace at the full release commit
   (`--revision`), install Jev from its published wheel URLs, and confirm the installed
   extension ID/version. The scoped downloaded catalog can alternatively be registered
   as a local snapshot with explicit marketplace trust; CLI 0.2.12 does not accept a
   remote JSON catalog as a Git marketplace source.
5. Record the tag/commit, release URL, artifact hashes, tested versions and reports.
   Only then replace the cookbook's unpublished-candidate instructions with verified
   public install instructions in a reviewed follow-up change.

Publication and mocked compatibility tests do not establish classification quality,
savings, latency or production readiness. Human label/decision review, calibration,
held-out evaluation and rollout approval remain application-owned gates.

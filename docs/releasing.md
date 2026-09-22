# Releasing Sanka extension packages

## Current candidate

`extensions-v0.1.0a31` bundles DRF-to-Flask 0.1.0a12, DRF-to-FastAPI
0.1.0a17, shared code migration helper 0.1.0a3 and DRF replay 0.1.0a4.
It adds qualified native Flask behavior, isolated PostgreSQL replay, and a
Python-mismatch error that points at reinstalling the CLI on the project's Python
(sankaHQ/sanka#127). The published SDK and connector wheels remain unchanged.

The candidate is not published. The merged converter revision `cad41241bd4e2ca9e741d1cdd5ac174c9217f66e`
passed [CI](https://github.com/sankaHQ/extensions/actions/runs/35169260045):
963 workspace tests, 106 installed-wheel tests, modern Django parity and
194 verified wheel hashes. These results cover the declared synthetic contracts;
they do not establish support for arbitrary application behavior.

Before publication, require a successful converter regression run at the exact
release commit, explicit publication authorization, and successful immutable SDK
verification. The current private benchmark exercises Flask's retained-Django
profile. Its result must not be described as standalone SQLAlchemy acceptance.
The broader Go entry gate in the backend migration plan also requires native
SQLAlchemy benchmark qualification; that work remains open. Do not mark the Go
gate complete merely because the marketplace release succeeds.

## Preparation and review

Use the repository uv workspace and focused checks while editing. Regenerate
manifest hashes with `make update-marketplace-hashes` after final package changes.
Finish code review, then let the workspace PR helper run `make check build-release`
as the final broad gate. The build verifies all 194 wheel filenames, locked
third-party hashes, package versions, dependency and entry-point boundaries, and
manifest URLs and hashes. The builder resumes interrupted dependency downloads by
reusing existing wheels only when their locked size and SHA-256 match; it always
rebuilds the local implementing packages. All previous release tags and artifacts
remain immutable.

Merge the exact human-approved head using `sanka-pr-flow`. Publication requires
user authorization separately from preparing the candidate. Create and push
`extensions-v0.1.0a31` at the reviewed merge, then dispatch `publish.yml` at that tag.
The workflow rejects every other tag. This repository currently publishes GitHub
release wheels; it does not publish these versions to PyPI.

## SDK before implementing packages

The publication workflow builds and verifies the complete bundle, then checks
that the existing `sdk-v0.1.0a4` tag still identifies its reviewed source and that
both SDK wheels in the bundle are byte-identical to their published assets.
The SDK verification job is read-only. It does not move the tag or re-upload
unchanged SDK packages. Only after verification succeeds may the marketplace
job publish implementing packages under `extensions-v0.1.0a31`.

A changed or missing SDK tag, local wheel or public asset blocks publication.
SDK changes require a separately reviewed version and publication before the
implementing package release. Do not replace existing assets to make this check pass.

After publication, verify the selected tag SHAs and GitHub artifact hashes, install
from the published assets in a clean environment, and exercise Data/Code and Flow
contract imports. Only then advance the shared runtime's SDK provenance and
immutable default marketplace revision. Runtime integration and cloud deployment
are separate changes; a parsed Blueprint is not proof of native workflow execution.

## Converter changes

For changes to converter behavior, complete the exact-commit
[converter regression check](converter-regression.md) in the private benchmark
repository and link its successful private run in the release review.

# Releasing Sanka extension packages

## Current candidate

`extensions-v0.1.0a25` prepares DRF-to-FastAPI 0.1.0a13 with bounded membership
permissions, user/parent-scoped queries and custom member/item writes. See
[the supported contracts and remaining compatibility gates](drf-membership-compatibility.md).
The SDK and other implementing packages remain unchanged.

## Preparation and review

Use the repository uv workspace and focused checks while editing. Regenerate
manifest hashes with `make update-marketplace-hashes` after final package changes.
Finish code review, then let the workspace PR helper run `make check build-release`
as the final broad gate. The build verifies all 193 wheel filenames, locked
third-party hashes, package versions, dependency and entry-point boundaries, and
manifest URLs and hashes. All previous release tags and artifacts remain immutable.

Merge the exact human-approved head using `sanka-pr-flow`. Publication requires
user authorization separately from preparing the candidate. Create and push
`extensions-v0.1.0a25` at the reviewed merge, then dispatch `publish.yml` at that tag.
The workflow rejects every other tag. This repository currently publishes GitHub
release wheels; it does not publish these versions to PyPI.

## SDK before implementing packages

The publication workflow builds and verifies the complete bundle, then checks
that the existing `sdk-v0.1.0a4` tag still identifies its reviewed source and that
both SDK wheels in the bundle are byte-identical to their published assets.
The SDK verification job is read-only. It does not move the tag or re-upload
unchanged SDK packages. Only after verification succeeds may the marketplace
job publish implementing packages under `extensions-v0.1.0a25`.

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

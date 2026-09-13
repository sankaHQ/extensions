# Releasing Sanka extension packages

## Published foundation and next candidate

`extensions-v0.1.0a19` publishes SDK 0.1.0a3 with Blueprint v2. The next candidate,
`extensions-v0.1.0a20`, adds SDK 0.1.0a4 with the typed Flow generator protocol.
It includes compatibility SDK 0.1.0a12, data-access extensions 0.1.0a14,
DRF-to-FastAPI 0.1.0a8, DRF-to-Flask 0.1.0a6 and DRF replay 0.1.0a2.
Implementing package versions change only to consume the new SDK without replacing
published artifacts. No runnable Flow marketplace package is included.

## Preparation and review

Use the repository uv workspace and focused checks while editing. Regenerate
manifest hashes with `make update-marketplace-hashes` after final package changes.
Finish code review, then let the workspace PR helper run `make check build-release`
as the final broad gate. The build verifies all 193 wheel filenames, locked
third-party hashes, package versions, dependency and entry-point boundaries, and
manifest URLs and hashes. All previous release tags and artifacts remain immutable.

Merge the exact human-approved head using `sanka-pr-flow`. Publication requires
user authorization separately from preparing the candidate. Create and push
`extensions-v0.1.0a20` at the reviewed merge, then dispatch `publish.yml` at that tag.
The workflow rejects every other tag. This repository currently publishes GitHub
release wheels; it does not publish these versions to PyPI.

## SDK before implementing packages

The publication workflow builds and verifies the complete bundle, then publishes
`sdk-v0.1.0a4` at the same reviewed source SHA with the SDK and its already-published
compatibility dependency. Only after that job succeeds may the marketplace job
publish the implementing packages and manifests under `extensions-v0.1.0a20`.
Consumers can install the SDK independently from the SDK release. The complete
marketplace also includes the same SDK wheel bytes for offline installation.

A failed marketplace upload does not authorize overwriting a published SDK or
release. Inspect the exact release assets and run state first. Rerun only the
failed job when the SDK job already succeeded; a full rerun intentionally refuses
to create an existing SDK release. Resolve a partial release explicitly.

After publication, verify the selected tag SHAs and GitHub artifact hashes, install
from the published assets in a clean environment, and exercise Data/Code and Flow
contract imports. Only then advance the shared runtime's SDK provenance and
immutable default marketplace revision. Runtime integration and cloud deployment
are separate changes; a parsed Blueprint is not proof of native workflow execution.

## Converter changes

For changes to converter behavior, complete the exact-commit
[converter regression check](converter-regression.md) in the private benchmark
repository and link its successful private run in the release review.

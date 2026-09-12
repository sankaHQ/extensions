# Releasing Sanka extension packages

## Current candidate

Publish one `extensions-v0.1.0a18` GitHub prerelease from the exact reviewed merged
source, after both the Flow contracts and Sales generator changes have landed.
The candidate includes these independently versioned packages:

| Package | Version |
| --- | --- |
| Compatibility SDK: `sanka-connector-sdk` | 0.1.0a12, unchanged |
| Unified SDK: `sanka-extension-sdk` | 0.1.0a3 |
| Sales Quote generator | 0.1.0a1 |
| Five Data extensions | 0.1.0a13 |
| DRF-to-FastAPI | 0.1.0a6 |
| DRF-to-Flask | 0.1.0a4 |
| DRF replay dependency | 0.1.0a1, unchanged |

Do not publish the intermediate Flow-contract SDK a3 and then change its bytes
when Sales lands. Review and publish their final combined SDK once. Existing
tags, package versions and asset bytes stay immutable. The current distribution
channel is GitHub release wheels; this workflow does not publish to PyPI or
TestPyPI, and it has no standalone publisher flags or bootstrap inputs.

## Preparation and review

Use Python 3.12 and the locked workspace. Run focused tests during development;
finish code review, then let `sanka-pr-flow open` own the final `make check` and
`make build-release` gates with the workspace resource guard. `make check` covers
the staged publisher's ordering, conflicting artifacts and partial-retry tests.

`make build-release` builds 194 wheels into `dist/` and validates their metadata,
dependency closures and immutable manifest hashes. It does not publish. After
intentional package-byte changes, prepare and review updated hashes with
`make update-marketplace-hashes`; never rewrite reviewed hashes during publishing.

From the candidate checkout, inspect the exact publication inventory without
contacting GitHub or creating a release:

```bash
uv run python scripts/publish_release.py \
  --tag extensions-v0.1.0a18 --revision <full-reviewed-source-sha> --dist dist
```

The plan contains 194 wheels and ten JSON assets: two catalogs and eight uniquely
named package manifests. Its digest binds the complete asset inventory to the
selected source commit. The historical family-version `check_release_tag.py`
script is not the validator for this independently versioned marketplace bundle.

## Authorized publication

Land the exact approved heads with the workspace `sanka-pr-flow`. Reconcile any
stacked branch before its final review. Obtain separate publication authorization
for the reviewed release, pin the final merged source SHA, and confirm the new
tag/release do not already exist. If they exist, inspect their exact identities
and partial state; do not replace them or blindly repeat tag creation.

Create the annotated a18 tag at that selected SHA and push it without force.
Dispatch the canonical workflow at the tag, with no additional inputs:

```bash
gh workflow run publish.yml --repo sankaHQ/extensions --ref extensions-v0.1.0a18
```

The workflow checks out the exact selected commit, runs locked validation and
builds the immutable artifacts. Its publishing job checks out the same commit
and restores the artifacts to their original paths. The source must remain
clean. Publishing requires the GitHub Actions tag/repository/SHA identity to
match, and the remote tag must resolve to that same commit before each stage.
Tag-scoped workflow concurrency prevents competing publications.

## Verified publication order

`scripts/publish_release.py --publish` runs only through the canonical tag
workflow. It creates one public prerelease with a source/inventory marker and
publishes in these stages:

1. The unchanged compatibility SDK. Existing same-version SDK assets across
   prior releases must match the reviewed bytes.
2. Unified SDK a3. Both SDK assets are downloaded from their public URLs and
   independently checked for exact size and SHA-256. A fresh isolated Python 3.12
   environment installs only those downloaded, hash-pinned wheels with no index
   or dependency fallback. Import, compatibility identity and Flow protocol
   checks must pass before any later stage can upload.
3. The replay package and all 183 locked third-party prerequisite wheels.
4. The eight implementing Sales, Data and Code extension wheels.
5. The two catalogs and eight uniquely named manifests, after every advertised
   wheel is available.

Each stage requires complete GitHub metadata readback and independent public
download/hash verification. The completed release has exactly 204 assets. An
unknown asset, conflicting SDK version, mismatched digest/size, changed source
tag, changed release identity or violated dependency order stops publication.

## Partial release recovery and completion

An interrupted run can leave an incomplete public prerelease. Its release body
states that completion requires the matching workflow and verified asset set.
Retry the same workflow at the same immutable tag. The helper accepts only the
same source/inventory marker and existing matching assets, downloads and verifies
them again, repeats the SDK installation proof, and uploads only missing assets.
An uncertain upload is resolved by readback on the next run. No asset is deleted
or overwritten; `--clobber`, tag movement and release recreation are not recovery.

The workflow retains `publication-evidence-<tag>-<attempt>` with the selected SHA,
inventory digest, verified stages and SDK installation result. Only an evidence
record with `outcome="complete"`, a successful exact-SHA workflow run and complete
public readback establish publication. A failed or interrupted attempt can retain
the last verified stage and must not be described as a completed release.

After the workflow, independently read the exact tag/run/release, download the
public wheels and run `scripts/check_release_artifacts.py <download-directory>`
from the final release checkout. Compare both catalogs and all package manifests
with the reviewed source. Fresh-install the published extension closures through
the shared CLI and verify Data/Code entry points and the real Sales generator.
SDK publication must precede runtime source/dependency upgrades. SDK generation
or package publication alone does not prove native execution or activate a Flow.

## Converter changes

For changed DRF-to-FastAPI behavior, follow the exact-commit private
[converter regression check](converter-regression.md) and link its successful
workflow in the release evidence. Keep benchmark fixtures and reports private.

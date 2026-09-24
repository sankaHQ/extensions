# Releasing Sanka extension packages

## FastAPI Swagger choice: a32

`extensions-v0.1.0a32` advances only `sanka-extension-drf-to-fastapi` to
`0.1.0a18`. Its reviewed Plan choice controls `/docs` in native and compatibility
output; `/openapi.json` and `/redoc` remain available. The other marketplace
wheels are byte-identical to their published versions. The FastAPI manifest points
to the a32 asset set; existing project locks and the a31 release remain unchanged.

Run `make check build-release`, then the private converter regression for the
exact reviewed merge SHA as described in [converter-regression.md](converter-regression.md).
After its success, confirm that the a32 tag and release are absent, create the
immutable `extensions-v0.1.0a32` tag at that merge, and dispatch `publish.yml`.
Verify the published a18 wheel and manifest hashes, SDK identity and a clean
FastAPI Plan with Swagger enabled and disabled before advancing the CLI catalog pin.

## CLI 0.3 compatibility update

The existing Data/Code wheel assets in `extensions-v0.1.0a31` remain immutable.
The reviewed marketplace manifests widen their CLI range through 0.3.x while
retaining support for 0.2.14. `sanka/llm-to-jev` retains its exact 0.2.12 pin;
it has not been qualified for 0.3. The separate Business Flow package advances
to `0.1.0a2` at `business-flows-v0.1.0a2` with the same SDK contract and a
manifest that accepts CLI 0.2.13 through 0.3.x. Publish a2 only after the
reviewed merge and exact-head checks. A new CLI can then pin the merged catalog
commit. Existing project locks keep their prior manifests until the user refreshes
the catalog and explicitly adds the extension again.

## Published a31 baseline

`extensions-v0.1.0a31` bundles DRF-to-Flask 0.1.0a12, DRF-to-FastAPI
0.1.0a17, shared code migration helper 0.1.0a3 and DRF replay 0.1.0a4.
It adds qualified native Flask behavior, isolated PostgreSQL replay, and a
Python-mismatch error that points at reinstalling the CLI on the project's Python
(sankaHQ/sanka#127). The published SDK and connector wheels remain unchanged.

The [a31 release](https://github.com/sankaHQ/extensions/releases/tag/extensions-v0.1.0a31)
was published from merge `6f49f7f30acdcad98ae26b05049591662307975b` after the
exact-merge converter regression. Its results cover declared synthetic contracts;
they do not establish support for arbitrary application behavior.

The private a31 regression exercises Flask's retained-Django
profile. Its result must not be described as standalone SQLAlchemy acceptance.
The broader Go entry gate in the backend migration plan also requires native
SQLAlchemy benchmark qualification; that work remains open. Do not mark the Go
gate complete merely because the marketplace release succeeds.

## Preparation and review

Use the repository uv workspace and focused checks while editing. Regenerate
manifest hashes only after wheel changes; the CLI 0.3 compatibility update
reuses the reviewed a31 Data/Code wheel bytes. Finish code review, then run `make check build-release`
as the final broad gate. The build verifies all 194 wheel filenames, locked
third-party hashes, package versions, dependency and entry-point boundaries, and
manifest URLs and hashes. The builder resumes interrupted downloads by
reusing published wheels only when their locked size and SHA-256 match. The a31
implementing wheels are pinned to their published bytes; source changes require new
package versions and release URLs. All previous release tags and artifacts
remain immutable.

Merge the exact human-approved head using `sanka-pr-flow`. Do not recreate the
existing a31 tag or release. After merge, the separately authorized Business Flow
a2 publication uses the new immutable `business-flows-v0.1.0a2` tag and
`publish-business-flows.yml`. This repository publishes GitHub release wheels,
not these versions to PyPI.

## a31 SDK provenance

The a31 publication workflow built and verified the complete bundle, then checked
that the existing `sdk-v0.1.0a4` tag still identifies its reviewed source and that
both SDK wheels in the bundle are byte-identical to their published assets.
The SDK verification job was read-only. It did not move the tag or re-upload
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

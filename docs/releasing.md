# Releasing Sanka extension packages

## Go lifecycle feedback: API a15

`api-converters-v0.1.0a15` publishes Python-to-Go `0.1.0a15`. Test records actual
Go test counts and HTTP scenario outcomes. Verify reports source/Go status,
response/database comparisons and endpoint coverage, including unexercised
methods. Deliberate progress messages use stderr; raw child logs and response
bodies are not streamed. A compatible CLI displays this feedback without mixing
it into JSON or compact output. Generation and migration bounds are unchanged.
The gadget fixture now covers PUT validation, replacement, readback and missing
records in the existing four-router lifecycle.

Build with `scripts/build_api_release.py --write-manifests`, review digests, and
require maintained checks, installed candidate acceptance and native qualification.
Publish the absent immutable tag from the reviewed merge. Validate the downloaded
bundle and public acceptance before advancing the next CLI release's official pin.
The shared SDK, helper and Rust wheels retain their existing versions and bytes.

## Automatic Go source discovery: API a14

`api-converters-v0.1.0a14` publishes Python-to-Go `0.1.0a14`. Scan detects the
framework, entrypoint, models and source database; Plan chooses router, destination
database and endpoint scope. Ambiguous or dynamic source inputs require an override.
The SQLite and PostgreSQL migration bounds remain unchanged. Other wheel versions
and bytes remain unchanged; the API manifests point at this new immutable bundle.

Build with `scripts/build_api_release.py --write-manifests`, review hashes, and
require maintained checks plus native router qualification. Publish from the landed
immutable tag, verify downloaded artifacts and public CLI lifecycles, then advance
the CLI default catalog pin. No marketplace wheel republication is needed.

## SQLite and DRF replay fixes: a36 / API a13

`extensions-v0.1.0a36` publishes DRF-to-Flask `0.1.0a15`, DRF-to-FastAPI
`0.1.0a21` and DRF replay `0.1.0a5`. Flask preserves the source missing-object
response; shared replay keeps FastAPI PostgreSQL URLs in its native driver format.
Both converters depend on the new replay wheel. SDK and code-migration wheels
retain their published bytes.

`api-converters-v0.1.0a13` publishes Python-to-Go `0.1.0a13`: bounded
SQLite → SQLite, SQLite → PostgreSQL and PostgreSQL → PostgreSQL migrations,
explicit existing-row transfer, rollback and numeric ordering on all four routers.
PostgreSQL → SQLite remains unsupported. Other API wheels retain their versions
and bytes. See [API release qualification](api-converter-release.md).

Run `make update-marketplace-hashes`, build the API bundle with
`--write-manifests`, review the hashes, then run the maintained checks and strict
artifact validators. Publish only absent immutable tags on the reviewed merge.
Verify clean downloaded bundles and installed public lifecycles before advancing
the CLI official catalog pin. Existing project locks remain unchanged until an
explicit extension upgrade.

## Plan settings declarations: a35

`extensions-v0.1.0a35` advances `sanka-extension-drf-to-fastapi` to `0.1.0a20` and
`sanka-extension-drf-to-flask` to `0.1.0a14`. Each wheel adds
`sanka-extension-settings.json` ([extension-settings.md](extension-settings.md)), which
declares its Plan settings for the Sanka CLI TUI and Sanka Code. Converter behavior is
unchanged. The other marketplace wheels are byte-identical to their published versions.

Run `make check build-release`, then the private converter regression for the exact
reviewed merge SHA. After its success, confirm that the a35 tag and release are absent,
create the immutable `extensions-v0.1.0a35` tag at that merge, and dispatch
`publish.yml`. Verify the published a20 and a14 wheel and manifest hashes, and that
each wheel contains its settings file, before advancing the CLI catalog pin.

## Retire local application data packages: a33

`extensions-v0.1.0a33` removes the five application data packages from the
current catalog and repository. The release bundle contains only the six
already published wheels used by the remaining code extensions. Their bytes and
manifest URLs stay pinned. Previously published data wheels and existing project
locks remain immutable; a new catalog snapshot no longer offers them for install.

The canonical SDK namespace changes to `sanka_extensions.app` in a separately
versioned a8 candidate. Publish that SDK before updating the shared runtime's
embedded SDK provenance. Run `make check build-release` on the reviewed source,
then verify the a33 catalog contains only current code extensions before a
separately authorized publication.

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

For unreleased source changes, use `make build-release RELEASE_ARGS=--candidate` to
validate current extension and replay wheels against the same package and dependency
contracts without changing versions or manifests. PR and main CI use this mode; published
SDK bytes remain pinned. Candidate success is not publication approval: the default
build and publication workflow still require every reviewed manifest hash to match.

API converter changes use `uv run python scripts/build_api_release.py --candidate`
in PR and main CI. This refreshes hashes only in the private output bundle; reviewed
source manifests and published SDK pins stay unchanged. Publication dispatches and
the default build remain strict and require coordinated new versions and assets.

Use the repository uv workspace and focused checks while editing. Regenerate
manifest hashes only after wheel changes. Finish code review, then run
`make check build-release` as the final broad gate. The build verifies the six
pinned wheel filenames, package versions, dependency and entry-point boundaries, and
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

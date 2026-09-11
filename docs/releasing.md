# Releasing Sanka extension packages

## Current marketplace release

Publish `extensions-v0.1.0a17` from the exact reviewed merge using `publish.yml`.
This bundle introduces `sanka_extensions` as the unified Sanka Extension SDK.
It includes SDK 0.1.0a2, compatibility SDK 0.1.0a12, system-access extensions
0.1.0a12, DRF-to-FastAPI 0.1.0a5, and DRF-to-Flask 0.1.0a3.
Its manifests must reference the new tag and match every staged wheel hash;
All previously published release tags remain immutable. Run `make check` and
`make update-marketplace-hashes` before review. After publication, verify the
GitHub artifact digests and install the SDK and all extensions with the published CLI.
Do not describe the default marketplace migration path as verified until those
clean installations succeed.

## Historical standalone package procedure

All six packages share one version and one reviewed source tag. Publishing is
manual; merges and tags do not upload packages automatically.

## Package order

Publish `sanka-connector-sdk` first, then `sanka-extension-sdk`, then implementing
extensions. Every new extension depends on the unified SDK; its compatibility
dependency is included in every manifest wheel set.

## Local preparation

```bash
uv sync --frozen --all-packages
make check
make build-release
uv run python scripts/check_release_tag.py v0.1.0a12 tag
```

`make build-release` writes per-package wheels and source distributions under
`release/packages/`, a combined set under `release/all/`, the exact source
commit, and SHA-256 hashes. It does not publish anything.

## Trusted publishing setup

Each PyPI and TestPyPI project must trust this exact identity:

- owner: `sankaHQ`
- repository: `extensions`
- workflow: `publish.yml`
- environment: `pypi` or `testpypi`

Create pending trusted publishers for projects that do not exist yet. Keep the
GitHub environments restricted to version tags. No long-lived PyPI token
belongs in repository secrets or local files.

Keep `SANKA_EXTENSIONS_PUBLISH_ENABLED` and
`SANKA_EXTENSIONS_BOOTSTRAP_ENABLED` absent or `false` outside an explicitly
approved publication window. The workflow refuses to upload without the
matching variable set to `true`.

## Publication gate

1. Merge the exact reviewed commit through `sanka-pr-flow`.
2. Create and push `v<version>` only after authorized-human approval of the
   commit and local artifact hashes.
3. Dispatch **Publish extension packages** against that exact tag, using
   TestPyPI first.
4. Clean-install the SDK and every extension from TestPyPI; verify entry-point
   discovery and provider-specific imports.
5. Obtain explicit approval for the production artifact hashes, then dispatch
   the PyPI target. PyPI versions are immutable.
6. Clean-install from PyPI and verify `sanka-connector-sdk` has zero runtime
   dependencies and each extension installs only its own client/driver stack.

The bootstrap targets publish one package for first-project creation. Bootstrap
the SDK before any extension and use the exact confirmation string shown by the
workflow input.

## Converter changes

Before releasing `sanka-extension-drf-to-fastapi`, complete the exact-commit
[converter regression check](converter-regression.md) in the private benchmark
repository and link its successful private run in the release review.
